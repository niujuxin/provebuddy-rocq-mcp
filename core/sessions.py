"""Core interactive session bookkeeping.

Split from upstream ``interactive.py`` (rocq-mcp @ 6983113d0844c0b7f987c79dab13988445109bfb): the import
cache, the state table, staleness tracking, and tactic-path
reconstruction.  ``run_*`` operations live in :mod:`core.interactive`.
The module registers ``_invalidate_import_cache`` /
``_state_invalidate_all`` on ``pet._pet_invalidation_hooks`` at import
time, exactly like upstream.  Only imports are re-pointed; function
bodies are byte-identical to upstream.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from core import pet as _pet
from core import workspace as _workspace

# Import cache
# ---------------------------------------------------------------------------

_MAX_IMPORT_CACHE_SIZE: int = 10


@dataclass
class _CachedImportContext:
    """Cached pytanque State after running a set of import commands."""

    state: Any
    imports_hash: str
    workspace: str
    pet_generation: int


_import_cache: dict[str, _CachedImportContext] = {}
_import_cache_generation: int = 0


def _get_or_create_import_state(
    pet: Any,
    workspace: str,
    import_commands: list[str],
    lifespan_state: dict[str, Any],
) -> Any:
    """Return a cached post-import pytanque State, creating if needed.

    Writes all *import_commands* to a cache ``.v`` file so that coq-lsp
    processes them natively, then calls ``get_state_at_pos`` at the end
    of the file.  Subsequent calls with the same imports and workspace
    return the cached State instantly (skipping import re-processing).
    """
    imports_key = hashlib.sha256("\n".join(import_commands).encode()).hexdigest()
    ws = str(Path(workspace).resolve())

    cached = _import_cache.get(imports_key)
    if (
        cached
        and cached.workspace == ws
        and cached.pet_generation == _import_cache_generation
    ):
        return cached.state

    # Build the cache file content from the import commands.  coq-lsp
    # will process these as part of the file, so ``get_state_at_pos``
    # at the end gives us the complete post-import state.
    cache_content = "\n".join(import_commands) + "\n" if import_commands else ""
    cache_file = Path(ws) / f"rocq_mcp_cache_{os.getpid()}_.v"
    file_changed = not cache_file.exists() or cache_file.read_text() != cache_content
    if file_changed:
        cache_file.write_text(cache_content)

    # The file must exist on disk before set_workspace so coq-lsp can
    # index it.  Force a workspace re-set when the file content changed
    # so coq-lsp picks up the updated imports.
    if file_changed:
        lifespan_state["current_workspace"] = None  # force re-set
    _pet._set_workspace_if_needed(pet, workspace, lifespan_state)

    # Position past the last line so all imports are in scope.
    # +1 ensures consistency with _get_file_end_state line counting.
    end_line = cache_content.count("\n") + 1
    state = pet.get_state_at_pos(str(cache_file), end_line, 0)

    _import_cache[imports_key] = _CachedImportContext(
        state=state,
        imports_hash=imports_key,
        workspace=ws,
        pet_generation=_import_cache_generation,
    )

    # Bound cache size (FIFO eviction)
    if len(_import_cache) > _MAX_IMPORT_CACHE_SIZE:
        del _import_cache[next(iter(_import_cache))]

    return state


def _get_file_end_state(
    pet: Any,
    file: str,
    workspace: str,
    lifespan_state: dict[str, Any],
) -> Any:
    """Get pytanque State at end of a ``.v`` file (all definitions in scope).

    Resolves the file path, validates workspace containment, sets the
    workspace, counts lines, and calls ``pet.get_state_at_pos`` past the
    last line.  The returned state has all imports, definitions, and
    notations from the file in scope.

    This is used by tools that accept a ``file`` parameter as an
    alternative to ``preamble`` (e.g., ``rocq_query``, ``rocq_assumptions``).

    Raises:
        ValueError: If the file path is outside the workspace.
        FileNotFoundError: If the file does not exist or is not readable.
    """
    resolved = _workspace._resolve_file_in_workspace(file, workspace)

    try:
        content = Path(resolved).read_text()
    except PermissionError:
        raise FileNotFoundError(f"File not accessible: {file}")

    # coq-lsp re-reads individual files on every get_state_at_pos call,
    # so the workspace itself only needs re-setting when the workspace
    # path changes — _set_workspace_if_needed handles that.  Eagerly
    # invalidating ``current_workspace`` here used to defeat that cache
    # on every sibling call against the same project.
    _pet._set_workspace_if_needed(pet, workspace, lifespan_state)

    # Position past the last line so all definitions are in scope.
    # +1 ensures files without a trailing newline still capture the last line.
    end_line = content.count("\n") + 1

    return pet.get_state_at_pos(resolved, end_line, 0)


def _invalidate_import_cache() -> None:
    """Clear all cached import states (called on pet crash/invalidation)."""
    global _import_cache_generation
    _import_cache.clear()
    _import_cache_generation += 1


# ---------------------------------------------------------------------------
# State table
# ---------------------------------------------------------------------------

_MAX_STATES: int = int(os.environ.get("ROCQ_MAX_STATES", "1000"))


@dataclass
class _StateEntry:
    """A proof state stored in the state table."""

    state: Any  # pytanque State
    file: str
    theorem: str
    workspace: str
    parent_id: int | None
    tactic: str | None
    step: int
    proof_finished: bool = False
    file_mtime: float | None = None  # mtime at session creation
    resolved_file: str | None = None  # absolute path for staleness check
    # Workspace .vo epoch when this session's environment was established
    # (inherited from the parent state so the whole lineage shares the root's
    # value).  Compared against the current epoch to detect a dependency .vo
    # rebuilt under a held session (see _check_staleness).  None for states
    # created without a lifespan_state (e.g. some tests).
    vo_epoch: int | None = None
    # Wall-clock timestamp; used by rocq_diag for age.
    created_at: float = field(default_factory=time.time)


# LRU-ordered: ``_state_get`` / ``_state_get_or_error`` move accessed
# entries to the most-recently-used end; eviction pops from the
# least-recently-used end.  Keeps actively-used states alive even when
# a parallel caller is churning through fresh states (e.g. two sub-agents
# on different files sharing one rocq-mcp process).
_state_table: "OrderedDict[int, _StateEntry]" = OrderedDict()
_state_next_id: int = 1


def _state_add(
    state: Any,
    file: str,
    theorem: str,
    workspace: str,
    parent_id: int | None,
    tactic: str | None,
    step: int,
    *,
    file_mtime: float | None = None,
    resolved_file: str | None = None,
    vo_epoch: int | None = None,
) -> int:
    """Add a state to the table and return its integer ID.

    *vo_epoch* stamps the workspace .vo epoch at session start.  A child
    state (``parent_id`` present in the table) inherits its parent's epoch so
    the whole lineage reflects when the root's environment was loaded — a
    child created *after* a dependency rebuild must not look fresh.
    """
    global _state_next_id
    sid = _state_next_id
    _state_next_id += 1
    parent = _state_table.get(parent_id) if parent_id is not None else None
    effective_epoch = parent.vo_epoch if parent is not None else vo_epoch
    _state_table[sid] = _StateEntry(
        state=state,
        file=file,
        theorem=theorem,
        workspace=workspace,
        parent_id=parent_id,
        tactic=tactic,
        step=step,
        proof_finished=getattr(state, "proof_finished", False),
        file_mtime=file_mtime,
        resolved_file=resolved_file,
        vo_epoch=effective_epoch,
    )
    # Evict LRU entries when table exceeds max size.
    while len(_state_table) > _MAX_STATES:
        _state_table.popitem(last=False)
    return sid


def _state_get(state_id: int) -> _StateEntry | None:
    """Look up a state by ID and promote it to most-recently-used.

    Returns None if not found.  Promotion is the read-side of LRU: a
    parked state that's still being queried by ``from_state=N`` survives
    eviction pressure from a parallel caller churning through new states.
    """
    entry = _state_table.get(state_id)
    if entry is not None:
        _state_table.move_to_end(state_id)
    return entry


def _state_remove(state_id: int) -> None:
    """Drop a state from the table."""
    _state_table.pop(state_id, None)


def _state_get_or_error(state_id: int) -> tuple[_StateEntry | None, str | None]:
    """Look up a state by ID, returning (entry, None) or (None, error_msg).

    On hit, promotes the entry to most-recently-used (same LRU semantics
    as ``_state_get``).
    """
    entry = _state_table.get(state_id)
    if entry is not None:
        _state_table.move_to_end(state_id)
        return entry, None
    # Distinguish eviction from never-existed
    if state_id < _state_next_id:
        return None, (
            f"State {state_id} expired: it aged out of the LRU table (no calls "
            f"to from_state={state_id} while many other states were active), or "
            f"pet was restarted (auto-recovery from a timeout or crash, or a "
            f"peer caller's force_restart=True).  Call rocq_start to begin a "
            f"fresh session — you do not need force_restart=True unless this "
            f"expiry repeats."
        )
    return None, f"State {state_id} does not exist."


def _state_invalidate_all() -> None:
    """Clear all states (called on pet crash/invalidation)."""
    _state_table.clear()


def _resolve_check_base_state(
    from_state: int,
) -> tuple["_StateEntry | None", int | None, str | None]:
    """Resolve the base state for ``run_check`` / friends.

    Returns ``(entry, base_state_id, error_message)``.  Exactly one of
    *error_message* / ``(entry, base_state_id)`` is set.  ``from_state``
    is required — there is no implicit "current state" fallback, which
    avoids a peer-caller hazard in shared-process deployments.
    """
    entry, err = _state_get_or_error(from_state)
    if err:
        return None, None, err
    return entry, from_state, None


def _check_staleness(
    entry: _StateEntry, lifespan_state: dict[str, Any] | None = None
) -> str | None:
    """Check whether a held state's environment may be stale.

    Two independent signals, checked in order:

    1. The state's own backing ``.v`` file changed on disk since session
       start (or became inaccessible).
    2. A *dependency* ``.vo`` in the workspace was rebuilt through this server
       since the session's environment was established — a held ``state_id``
       freezes the loaded libraries, so ``proof_finished`` on such a state
       can diverge from a clean compile.  Detected via the per-workspace
       ``.vo`` epoch; requires *lifespan_state*.

    Returns a warning message on the first hit, or None if fresh.  Preamble
    states (no backing file) skip check 1 but are still covered by check 2.
    """
    if entry.resolved_file is not None and entry.file_mtime is not None:
        try:
            current_mtime = os.path.getmtime(entry.resolved_file)
        except OSError:
            return (
                f"File '{entry.file}' is no longer accessible. "
                f"The proof state may be stale. "
                f"Use rocq_start to begin a fresh session."
            )
        if current_mtime != entry.file_mtime:
            return (
                f"File '{entry.file}' has been modified since session start. "
                f"The proof state may be stale. "
                f"Use rocq_start to begin a fresh session."
            )
    if (
        lifespan_state is not None
        and entry.vo_epoch is not None
        and entry.workspace
        and _workspace._current_vo_epoch(lifespan_state, entry.workspace) > entry.vo_epoch
    ):
        return (
            f"A dependency .vo in workspace '{entry.workspace}' was rebuilt "
            f"since this session started, so its held environment may be stale "
            f"and 'proof_finished' here can diverge from a clean compile. "
            f"Re-verify with rocq_compile_file, or rocq_start for a fresh session."
        )
    return None


@dataclass(frozen=True)
class _TacticPathResult:
    """Outcome of walking the ``parent_id`` chain back from a leaf state.

    ``tactics`` is in root→leaf order and is only meaningful when
    ``status == "complete"``.  On a broken walk it holds whatever was
    collected before the break; the caller should not surface it as a
    finished tactic chain.

    ``status`` is one of:

    - ``"complete"`` — walk reached the root (``parent_id`` is ``None``)
    - ``"ancestor_evicted"`` — an ancestor entry was missing from the
      state table (LRU eviction or pet restart)
    - ``"cycle"`` — a ``current_id`` reappeared during the walk

    ``broken_at`` is the ``current_id`` at the break point in the
    ``ancestor_evicted`` / ``cycle`` cases, and ``None`` when complete.
    """

    tactics: list[str]
    status: Literal["complete", "ancestor_evicted", "cycle"]
    broken_at: int | None


def _reconstruct_tactic_path(state_id: int) -> _TacticPathResult:
    """Walk the parent_id chain backward; return a ``_TacticPathResult``.

    See ``_TacticPathResult`` for the meaning of each field and the
    three possible status values.
    """
    tactics: list[str] = []
    current_id: int | None = state_id
    visited: set[int] = set()
    status: Literal["complete", "ancestor_evicted", "cycle"] = "complete"
    broken_at: int | None = None
    while current_id is not None:
        if current_id in visited:
            status = "cycle"
            broken_at = current_id
            break
        visited.add(current_id)
        entry = _state_get(current_id)
        if entry is None:
            status = "ancestor_evicted"
            broken_at = current_id
            break
        if entry.tactic is not None:
            tactics.append(entry.tactic)
        current_id = entry.parent_id
    tactics.reverse()
    return _TacticPathResult(tactics=tactics, status=status, broken_at=broken_at)


# ---------------------------------------------------------------------------
# Register pet invalidation hooks
# ---------------------------------------------------------------------------
# These are called by _invalidate_pet() in server.py whenever pet is killed
# (timeout, crash).  All cached State objects become invalid when pet dies.

_pet._pet_invalidation_hooks.append(_invalidate_import_cache)
_pet._pet_invalidation_hooks.append(_state_invalidate_all)


# ---------------------------------------------------------------------------
