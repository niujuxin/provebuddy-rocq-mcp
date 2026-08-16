"""Core pet management: subprocess lifecycle, locks, semaphore, memory watchdog.

Extracted verbatim from upstream ``server.py`` sections *Pet subprocess
management*, *Semaphore (shared by interactive tools)*, and the memory
watchdog / ``_run_with_pet`` block (rocq-mcp @
6983113d0844c0b7f987c79dab13988445109bfb).  Imports are re-pointed to
``core.config`` / ``core.envelope`` / ``core.workspace``; function bodies
and constants are unchanged.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

import psutil

from core import config as _config
from core.envelope import _fail, _merge_partial_state, _record_error
from core.workspace import _parse_project_flags

# --- Begin verbatim upstream content ---

# ---------------------------------------------------------------------------
# Pet subprocess management
# ---------------------------------------------------------------------------

# Global lock for ALL pytanque operations. Pytanque's stdio pipe is
# single-duplex -- concurrent reads/writes corrupt JSON-RPC framing.
# NOTE: _pet_lock may be replaced after a timeout (see _force_release_pet_lock).
# All _execute functions must capture a local reference before acquiring.
_pet_lock = threading.Lock()

# Callbacks invoked when pet is invalidated (crash, timeout).
# interactive.py registers _invalidate_import_cache and _state_invalidate_all
# here to break the circular dependency (server -> interactive -> server).
_pet_invalidation_hooks: list[Callable[[], None]] = []


class _PetLockTimeout(Exception):
    """Lock acquisition timed out (distinct from asyncio.TimeoutError).

    On Python 3.11+, TimeoutError *is* asyncio.TimeoutError. Using a
    private class prevents lock contention from being caught by the
    asyncio.wait_for timeout handler, which would incorrectly kill pet
    and destroy the proof session.
    """


async def _force_release_pet_lock() -> None:
    """Recover from a deadlocked _pet_lock after timeout.

    After _invalidate_pet kills the pet process, the orphaned thread's
    blocking pet.run() should fail and release the lock.  We wait briefly
    for this natural release.  If the lock is still held after a grace
    period, replace the global lock with a fresh one so subsequent
    operations can proceed.

    This is safe because every _execute function captures a local
    reference to the lock before acquiring it, so the orphaned thread
    releases its own (now-discarded) lock object.

    Runs the blocking acquire in a thread to avoid stalling the event loop.
    """

    def _try_reacquire() -> bool:
        lock = _pet_lock  # capture local ref
        if lock.acquire(timeout=2):
            lock.release()
            return True
        return False

    global _pet_lock
    if await asyncio.to_thread(_try_reacquire):
        return
    # Orphaned thread still holds the lock -- replace with fresh lock
    _pet_lock = threading.Lock()


def _ensure_pet(lifespan_state: dict[str, Any]) -> Any:
    """Lazy-initialize pet subprocess. Must be called with _pet_lock held."""
    try:
        from pytanque import Pytanque, PytanqueMode
    except ImportError:
        raise ImportError(_config._PYTANQUE_NOT_INSTALLED_HINT)

    pet = lifespan_state.get("pet_client")
    if pet is None or not _pet_alive(pet):
        if pet is not None:
            _kill_pet(pet)  # Full cleanup: kill + wait + close FDs
            for hook in _pet_invalidation_hooks:
                hook()
        pet = Pytanque(mode=PytanqueMode.STDIO)
        pet.connect()
        # Attempt process group setup for clean kill.
        # May fail on macOS if child already exec'd -- that's OK,
        # os.getpgid at kill time handles it.
        if pet.process:
            try:
                os.setpgid(pet.process.pid, pet.process.pid)
                pet._own_pgrp = True
            except OSError:
                pet._own_pgrp = False
        else:
            pet._own_pgrp = False
        # Count *only* successful spawns so a fresh server reports
        # pet_restarts=0 even if a previous spawn raised before reaching
        # this point.  The bookkeeping below is the canonical "spawn
        # succeeded" point.
        prev_spawns: int = int(lifespan_state.get("total_spawns", 0))
        if prev_spawns > 0:
            # Reset peak RSS so "headroom before vm_compute" reflects the
            # live pet, not a long-dead predecessor that may have pushed
            # the peak high.  Reset *before* assigning ``pet_client`` so
            # the watchdog cannot sample the new pet's RSS, write it to
            # ``peak_pet_rss_mb``, and then have us wipe it.
            lifespan_state["peak_pet_rss_mb"] = 0.0
        lifespan_state["pet_client"] = pet
        lifespan_state["pet_started_at"] = time.time()
        lifespan_state["total_spawns"] = prev_spawns + 1
    return pet


def _pet_alive(pet: Any) -> bool:
    """Check if the pet subprocess is still running."""
    return pet is not None and pet.process is not None and pet.process.poll() is None


def _kill_pet(pet: Any) -> None:
    """Kill pet and its entire process group.

    If the pet has its own process group (_own_pgrp=True), uses os.killpg
    to kill the whole group (pet + coq-lsp). Otherwise falls back to
    process.terminate()/kill() to avoid killing our own process group.
    """
    if pet is None or pet.process is None:
        return
    # If process already exited, just close FDs — no signals needed.
    # This avoids PID-reuse races where os.killpg could kill an unrelated process.
    if pet.process.poll() is not None:
        _try_close_pet(pet)
        return
    try:
        if getattr(pet, "_own_pgrp", False):
            # Safe: pet has its own process group
            pgid = os.getpgid(pet.process.pid)
            os.killpg(pgid, signal.SIGTERM)
        else:
            # Fallback: only kill the direct child
            pet.process.terminate()
        try:
            pet.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            if getattr(pet, "_own_pgrp", False):
                pgid = os.getpgid(pet.process.pid)
                os.killpg(pgid, signal.SIGKILL)
            else:
                pet.process.kill()
            pet.process.wait(timeout=3)
    except (OSError, ChildProcessError, subprocess.TimeoutExpired):
        # Process already dead, group doesn't exist, or refused to die
        pass
    # Close pipe file descriptors
    _try_close_pet(pet)


def _try_close_pet(pet: Any) -> None:
    """Close pytanque's pipe file descriptors without killing."""
    if pet is None or pet.process is None:
        return
    for stream in [pet.process.stdin, pet.process.stdout, pet.process.stderr]:
        try:
            if stream:
                stream.close()
        except Exception:
            # Best-effort FD cleanup -- the pipe may already be closed,
            # the peer may be dead, or the buffer may be in any state.
            # Any further error here is uninteresting; we only care that
            # we tried to close every FD we hold.
            pass


def _invalidate_pet(lifespan_state: dict[str, Any]) -> None:
    """Kill pet and set to None so next call respawns.

    Does NOT acquire _pet_lock — this is intentional. After a timeout,
    an orphaned thread may still hold the lock. The OS-level kill is safe
    to call without the lock (it's a signal, not a protocol operation).
    The next _ensure_pet call (under _pet_lock) will see the dead process
    and respawn.

    Note: there is a brief race window where a concurrent _ensure_pet
    call may have already read pet_client before this function sets it
    to None.  The stale pet object will fail with a broken-pipe error,
    which is caught by the caller's broad exception handler and triggers
    a respawn on the next call.
    """
    pet = lifespan_state.get("pet_client")
    if pet:
        _kill_pet(pet)
    lifespan_state["pet_client"] = None
    lifespan_state["current_workspace"] = None
    lifespan_state["pet_generation"] = lifespan_state.get("pet_generation", 0) + 1
    for hook in _pet_invalidation_hooks:
        hook()


def _set_workspace_if_needed(
    pet: Any, workspace: str, lifespan_state: dict[str, Any]
) -> None:
    """Set pet workspace, skipping if already set to the same directory.

    Side-effect: invokes :func:`_parse_project_flags` before
    ``pet.set_workspace`` so that any dune-derived ``_RocqProject`` is
    materialised on disk *before* coq-lsp indexes the workspace.
    Without this, pet-based tools on a fresh dune workspace would see a
    workspace with no project file, falling back to single-theory load
    paths and breaking cross-theory imports (an upstream pytanque limitation).
    """
    ws = str(Path(workspace).resolve())
    if lifespan_state.get("current_workspace") != ws:
        _parse_project_flags(Path(ws))
        pet.set_workspace(debug=False, dir=ws)
        lifespan_state["current_workspace"] = ws


# ---------------------------------------------------------------------------
# Semaphore (shared by interactive tools)
# ---------------------------------------------------------------------------

# Async-level serialization to prevent deadlock on timeout.
# Unlike threading.Lock, asyncio.Semaphore is released even when the
# thread is orphaned by asyncio.wait_for timeout.
# Shared across ALL pet operations (step + query) because pytanque's
# stdio pipe is single-duplex.
_pet_semaphore: asyncio.Semaphore | None = None


def _get_pet_semaphore() -> asyncio.Semaphore:
    """Lazy-init the semaphore (must be created inside a running event loop)."""
    global _pet_semaphore
    if _pet_semaphore is None:
        _pet_semaphore = asyncio.Semaphore(1)
    return _pet_semaphore


async def _build_memory_abort_response(
    lifespan_state: dict[str, Any],
    tool: str,
    on_timeout: Callable[[], None] | None,
    partial_state: dict[str, Any] | None,
    *,
    auto_record: bool = True,
) -> dict[str, Any]:
    """Run the memory-abort recovery path and return the response dict.

    Thin wrapper around :func:`_handle_pet_failure` that supplies the
    memory-specific error message; the recovery scaffold (invalidate
    pet, release lock, fire on_timeout, merge partial, record error)
    is shared with every other killed-pet path.  When *auto_record* is
    False, the ``recent_errors`` push is suppressed so the caller can
    classify the failure at its own layer.
    """
    return await _handle_pet_failure(
        lifespan_state,
        tool,
        reason="memory_exhausted",
        error=(
            f"{tool} aborted: pet RSS exceeded "
            f"{_config.ROCQ_MAX_PET_RSS_MB} MB. The proof state was lost; "
            "pet has been restarted. Retry with a smaller term, "
            "avoid vm_compute on large inputs, or split the work."
        ),
        killed_pet=True,
        on_timeout=on_timeout,
        auto_record=auto_record,
        partial_state=partial_state,
    )


async def _memory_watchdog(
    lifespan_state: dict[str, Any],
    max_rss_mb: int,
    main_task: asyncio.Task,
    event: asyncio.Event,
    interval: float | None = None,
) -> None:
    """Sample pet RSS; on threshold breach, set *event* and cancel *main_task*.

    Runs concurrently with ``_run_with_pet``'s main thread.  When the pet
    subprocess RSS exceeds ``max_rss_mb`` MB, signals memory exhaustion via
    *event* and cancels the main task so the existing timeout-class recovery
    path can reclaim the lock and respawn pet.

    Tolerates:
    - ``psutil`` not installed -- exits silently (no monitoring).
    - pet not yet spawned (``lifespan_state["pet_client"] is None``) --
      keeps polling.
    - pet exits between samples (``psutil.NoSuchProcess``) -- treated as
      transient; keeps polling.
    """
    if interval is None:
        interval = _config._MEMORY_WATCHDOG_INTERVAL

    try:
        while not main_task.done():
            await asyncio.sleep(interval)
            if main_task.done():
                return
            client = lifespan_state.get("pet_client")
            if client is None or client.process is None:
                continue
            try:
                pid = client.process.pid
                rss_bytes = psutil.Process(pid).memory_info().rss
            except (psutil.Error, AttributeError, OSError):
                # psutil.Error covers NoSuchProcess / AccessDenied / ZombieProcess;
                # OSError catches raw ProcessLookupError if pet died between
                # Process() construction and memory_info().
                continue
            rss_mb = rss_bytes // (1024 * 1024)
            if rss_mb > lifespan_state.get("peak_pet_rss_mb", 0):
                lifespan_state["peak_pet_rss_mb"] = float(rss_mb)
            if rss_mb > max_rss_mb:
                event.set()
                main_task.cancel()
                return
    except asyncio.CancelledError:
        return


async def _handle_pet_failure(
    lifespan_state: dict[str, Any],
    tool: str,
    *,
    reason: str,
    error: str,
    killed_pet: bool = False,
    on_timeout: Callable[[], None] | None = None,
    partial_state: dict[str, Any] | None = None,
    auto_record: bool = True,
) -> dict[str, Any]:
    """Build a unified failure response for ``_run_with_pet``'s except arms.

    When *killed_pet* is True (timeout / dead PetanqueError / BrokenPipe /
    ConnectionError), invalidates the pet client, force-releases the
    pet lock, optionally invokes the caller's *on_timeout* hook, and
    tags the response with ``pet_restarted: True``.  When False
    (lock contention / live PetanqueError / FileNotFoundError /
    unexpected OSError-class exception), leaves the pet alone.

    Both paths merge *partial_state* (if any) into the response and
    record the failure into ``recent_errors`` so ``rocq_diag`` surfaces
    it.  When *auto_record* is False, the ``_record_error`` call is
    skipped; the recovery side-effects (invalidate / lock release /
    on_timeout) still fire so the pet stays healthy.  Used by callers
    that need to classify the failure at their own layer (e.g.
    ``run_assumptions`` distinguishing ``not_found`` from generic crash).
    """
    if killed_pet:
        _invalidate_pet(lifespan_state)
        await _force_release_pet_lock()
        if on_timeout is not None:
            on_timeout()
    resp: dict[str, Any] = {"success": False, "error": error, "reason": reason}
    if killed_pet:
        resp["pet_restarted"] = True
    if partial_state:
        _merge_partial_state(resp, partial_state)
    if auto_record:
        _record_error(lifespan_state, tool, error, reason=reason)
    return resp


async def _run_with_pet(
    fn: Callable[[Any], Any],
    lifespan_state: dict[str, Any],
    tool: str,
    on_timeout: Callable[[], None] | None = None,
    timeout: float | None = None,
    partial_state: dict[str, Any] | None = None,
    *,
    auto_record: bool = True,
) -> Any:
    """Run *fn(pet)* with the pet client, handling lock/semaphore/timeout/errors.

    The helper encapsulates the full boilerplate shared by every pytanque
    operation that follows the simple "acquire lock, ensure pet, do work"
    pattern:

    1. PetanqueError import check
    2. _pet_lock acquisition with timeout
    3. _ensure_pet (lazy-init the pet subprocess)
    4. asyncio.Semaphore + asyncio.wait_for (async-level timeout)
    5. All standard exception handlers

    *fn* receives the live pet client and must return the desired result.
    It runs inside a background thread with _pet_lock held; the lock is
    released automatically when *fn* returns or raises.

    *tool* is the canonical MCP tool name (e.g. ``"rocq_check"``) and is
    used both as the prefix in user-facing error messages
    (``"rocq_check timed out after 30s."``) and as the ``tool`` field on
    ``recent_errors`` entries.  Pass exactly the public tool name, not a
    human phrase.

    When pet crashes (timeout, broken pipe), the return dict includes
    ``"pet_restarted": True`` so callers can decide whether to retry.

    If *partial_state* is given (a mutable dict), *fn* can populate it
    with intermediate results.  On timeout or error the dict contents
    are merged into the error response so partial work is not lost.

    When *auto_record* is False (default True), failure paths still
    return the same failure-envelope dict but skip the ``_record_error``
    push into ``recent_errors``.  Callers that need to classify the
    failure at their own layer (e.g. ``run_assumptions`` distinguishing
    ``not_found`` from a generic ``rocq_query/crashed``) pass
    ``auto_record=False`` to avoid the double-record / compensating-pop
    dance.

    The return type is left as ``Any`` because the dict shape varies by
    failure mode (success path, ``pet_restarted``-tagged crashes,
    ``partial_state`` merges, etc.) and a TypedDict would be unwieldy.
    The ``"reason"`` key, when present on a failure, is a
    :data:`compile_enrichment._StateCaptureStatus` (one of ``"timeout"``, ``"crashed"``,
    ``"memory_exhausted"``, ``"lock_contended"``, ``"unavailable"``).
    """
    try:
        from pytanque import PetanqueError
    except ImportError:
        return _fail(
            lifespan_state if auto_record else None,
            tool,
            _config._PYTANQUE_NOT_INSTALLED_HINT,
            "unavailable",
        )

    _timeout: float = timeout if timeout is not None else lifespan_state["pet_timeout"]
    # Lock acquire uses a shorter timeout than wait_for so that
    # _PetLockTimeout fires before asyncio.TimeoutError on contention.
    # This avoids unnecessarily killing pet when the issue is just
    # lock contention, not a pet hang.
    lock_timeout = _timeout * 0.8

    def _execute() -> Any:
        lock = _pet_lock  # capture local ref (survives _force_release_pet_lock)
        if not lock.acquire(timeout=lock_timeout):
            raise _PetLockTimeout("Could not acquire pet lock")
        try:
            pet = _ensure_pet(lifespan_state)
            return fn(pet)
        finally:
            lock.release()

    sem = _get_pet_semaphore()
    async with sem:
        main_task = asyncio.create_task(asyncio.to_thread(_execute))
        mem_event = asyncio.Event()
        monitor_task = asyncio.create_task(
            _memory_watchdog(lifespan_state, _config.ROCQ_MAX_PET_RSS_MB, main_task, mem_event)
        )
        try:
            try:
                result = await asyncio.wait_for(main_task, timeout=_timeout)
                return result
            finally:
                if not monitor_task.done():
                    monitor_task.cancel()
                    try:
                        await monitor_task
                    except asyncio.CancelledError:
                        pass
        except asyncio.CancelledError:
            # If mem_event is set, the watchdog cancelled main_task because
            # pet RSS exceeded the threshold; otherwise this is an external
            # cancel that should propagate.
            if mem_event.is_set():
                return await _build_memory_abort_response(
                    lifespan_state,
                    tool,
                    on_timeout,
                    partial_state,
                    auto_record=auto_record,
                )
            raise
        except asyncio.TimeoutError:
            # If the wait_for timer and the watchdog raced, mem_event may
            # already be set; prefer the more specific memory_exhausted label.
            if mem_event.is_set():
                return await _build_memory_abort_response(
                    lifespan_state,
                    tool,
                    on_timeout,
                    partial_state,
                    auto_record=auto_record,
                )
            return await _handle_pet_failure(
                lifespan_state,
                tool,
                reason="timeout",
                error=(
                    f"{tool} timed out after {_timeout}s. "
                    f"Retry with `{tool}(..., timeout=<seconds>)` "
                    f"(e.g. `timeout=180` for files with heavy library imports). "
                    f"Server-side defaults: ROCQ_PET_TIMEOUT (base, default 30s), "
                    f"ROCQ_QUERY_TIMEOUT_CAP (cap, default 300s). "
                    f"If the response also includes `clamped_timeout`, you have "
                    f"hit `ROCQ_QUERY_TIMEOUT_CAP`; call `rocq_diag` for memory "
                    f"headroom and consider `rocq_start(..., force_restart=True)` "
                    f"instead of bumping further."
                ),
                killed_pet=True,
                on_timeout=on_timeout,
                partial_state=partial_state,
                auto_record=auto_record,
            )
        except _PetLockTimeout:
            return await _handle_pet_failure(
                lifespan_state,
                tool,
                reason="lock_contended",
                error=f"{tool}: pet is busy (lock contention). Try again.",
                auto_record=auto_record,
            )
        except PetanqueError as e:
            if not _pet_alive(lifespan_state.get("pet_client")):
                return await _handle_pet_failure(
                    lifespan_state,
                    tool,
                    reason="crashed",
                    error=f"Pet process died: {e.message}",
                    killed_pet=True,
                    partial_state=partial_state,
                    auto_record=auto_record,
                )
            return await _handle_pet_failure(
                lifespan_state,
                tool,
                reason="crashed",
                error=e.message,
                auto_record=auto_record,
            )
        except (BrokenPipeError, ConnectionError) as e:
            return await _handle_pet_failure(
                lifespan_state,
                tool,
                reason="crashed",
                error=f"Pet process died: {e}",
                killed_pet=True,
                on_timeout=on_timeout,
                partial_state=partial_state,
                auto_record=auto_record,
            )
        except FileNotFoundError:
            return await _handle_pet_failure(
                lifespan_state,
                tool,
                reason="unavailable",
                error="pet binary not found on PATH. Install coq-lsp.",
                auto_record=auto_record,
            )
        except (OSError, RuntimeError, ValueError, TypeError) as e:
            return await _handle_pet_failure(
                lifespan_state,
                tool,
                reason="crashed",
                error=f"Unexpected error: {e}",
                partial_state=partial_state,
                auto_record=auto_record,
            )
