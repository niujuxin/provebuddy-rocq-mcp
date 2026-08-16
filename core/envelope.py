"""Core envelope helpers: failure responses, reason sets, MCP-agnostic wrapper glue.

Extracted verbatim from upstream ``server.py`` (the envelope-helper block
inside section *Semaphore (shared by interactive tools)*; rocq-mcp @
6983113d0844c0b7f987c79dab13988445109bfb).  ``_resolve_tool_envelope`` /
``_finalize_tool_envelope`` are kept even though the MCP wrappers are gone
so the envelope contract survives for the future MCP layer; they do not
depend on fastmcp.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from core import config as _config
from core.workspace import (
    _find_project_root_from_file,
    _maybe_workspace_warning,
    _resolve_call_timeout,
    _validate_workspace,
)

# --- Begin verbatim upstream content ---
def _merge_partial_state(resp: dict[str, Any], partial: dict[str, Any]) -> None:
    """Merge *partial* into *resp* without overwriting control keys.

    Keys like ``"success"``, ``"error"``, and ``"pet_restarted"`` are set by
    the error handler and must not be clobbered by user-provided partial state.
    """
    for k, v in partial.items():
        if k not in resp:
            resp[k] = v


_RECENT_ERROR_MESSAGE_LIMIT: int = 500

# Failure reasons emitted by ``_run_with_pet``'s except arms — the
# intersection of :data:`rocq_mcp.interactive._TRANSPORT_FAILURE_REASONS`
# and the failure subset of
# :data:`rocq_mcp.compile_enrichment._StateCaptureStatus`.  Single source
# of truth so the three reason-sets cannot drift apart silently.
_PET_SIDE_FAILURE_REASONS: frozenset[str] = frozenset(
    {
        "timeout",
        "crashed",
        "memory_exhausted",
        "lock_contended",
        "unavailable",
    }
)

# Allowed values for the ``reason`` field on ``recent_errors`` entries.
# A superset of :data:`compile_enrichment._StateCaptureStatus`'s failure modes plus
# ``"validation"`` for early-return validation failures, ``"not_found"``
# for name-resolution failures (rocq_start / rocq_assumptions typos),
# and the rocq_verify-specific reasons.
_RECENT_ERROR_REASONS: frozenset[str] = _PET_SIDE_FAILURE_REASONS | frozenset(
    {
        "validation",
        "not_found",
        # rocq_check mid-batch failure (a tactic was rejected by Coq).
        "tactic_failed",
        # rocq_verify-specific reasons (see compile.run_verify).
        "compile_error",
        "axiom_dependency",
        "type_mismatch",
    }
)

assert _PET_SIDE_FAILURE_REASONS <= _RECENT_ERROR_REASONS


def _record_error(
    lifespan_state: dict[str, Any] | None,
    tool: str,
    message: str,
    reason: str,
) -> None:
    """Append an entry to the ``recent_errors`` ring buffer.

    Stores absolute ``occurred_at`` timestamps; ``ago_seconds`` is computed
    lazily by ``_build_diag_snapshot`` so values stay fresh when the buffer
    is read.

    *tool* is the canonical MCP tool name (e.g. ``"rocq_check"``) and
    matches the output schema key in ``_build_diag_snapshot``.

    *reason* is one of :data:`_RECENT_ERROR_REASONS` — typically a
    :data:`compile_enrichment._StateCaptureStatus` value for pet-level failures, or
    ``"validation"`` for early-return validation failures.

    Long *message* strings are truncated to
    ``_RECENT_ERROR_MESSAGE_LIMIT`` chars + ``"..."`` to keep the
    ``rocq_diag`` payload bounded; the full message is preserved in the
    immediate response of the failing tool call.

    Tolerates ``lifespan_state is None`` (no recording) and missing
    ``recent_errors`` key (no recording) — both happen when the failing
    tool call has no MCP context.

    Asserts that *reason* is in :data:`_RECENT_ERROR_REASONS`.  Without
    this guard a typo'd reason would silently appear in ``rocq_diag``
    output and break agent dispatch logic — mirrors
    :data:`compile_enrichment._VALID_STATE_CAPTURE_STATUSES` which is used the same way
    in ``compile_enrichment``.
    """
    assert (
        reason in _RECENT_ERROR_REASONS
    ), f"unknown error reason {reason!r}; add it to _RECENT_ERROR_REASONS"
    if lifespan_state is None:
        return
    buf = lifespan_state.get("recent_errors")
    if buf is None:
        return
    if message is not None and len(message) > _RECENT_ERROR_MESSAGE_LIMIT:
        message = message[:_RECENT_ERROR_MESSAGE_LIMIT] + "..."
    buf.append(
        {
            "tool": tool,
            "message": message,
            "reason": reason,
            "occurred_at": time.time(),
        }
    )


def _fail(
    lifespan_state: dict[str, Any] | None,
    tool: str,
    message: str,
    reason: str = "validation",
    **extra: Any,
) -> dict[str, Any]:
    """Build a failure response dict and record it in ``recent_errors``.

    Convenience for the ``return {"success": False, "error": msg}`` pattern
    that also needs to push the error onto the diag ring buffer.  Skips
    recording when *lifespan_state* is ``None`` (no MCP context) so test
    helpers and pre-context paths stay simple.

    Always includes ``reason`` in the response so the unified envelope
    is consistent across pet-side failures (set by ``_run_with_pet``)
    and pre-pet validation failures (set here).
    """
    _record_error(lifespan_state, tool=tool, message=message, reason=reason)
    return {"success": False, "error": message, "reason": reason, **extra}


def _no_ctx_fail(tool: str) -> dict[str, Any]:
    """Canonical "no MCP context" failure envelope for tool wrappers.

    Routes through :func:`_fail` so the response shape and the
    ``recent_errors`` side-effect policy stay defined in one place; when
    ``ctx is None`` we have no ``lifespan_state`` to record into, so
    ``_fail`` no-ops the buffer write (same policy as every other
    ``lifespan_state is None`` caller).
    """
    return _fail(None, tool, "Internal error: no MCP context.")


def _resolve_tool_envelope(
    *,
    tool: str,
    ctx: Any,
    workspace: str,
    file: str | None = None,
    timeout: int | float | None = None,
    timeout_default: int | None = None,
    ctx_optional: bool = False,
) -> dict[str, Any] | tuple[str, dict[str, Any] | None, str | None, bool, float | None]:
    """Resolve the shared envelope steps for ``@mcp.tool`` wrappers.

    Runs the boilerplate every pet-routed / coqc-routed wrapper repeats:

    1. ctx-check first — for pet-routed tools, a missing context is a
       programmer error and is reported as the no-ctx envelope without
       touching the workspace (``_no_ctx_fail``).  Pins the order so a
       bad workspace + no-ctx caller gets the no-ctx envelope, not a
       silent validation failure that ``recent_errors`` never sees.
       Coqc-routed tools (``rocq_compile`` / ``rocq_compile_file`` /
       ``rocq_verify``) pass ``ctx_optional=True``: they can run without
       an MCP context — ``lifespan_state`` falls through as ``None`` and
       the ``recent_errors`` recording is silently skipped.
    2. Workspace resolution: explicit > project-marker walk-up from
       *file* (when *file* is non-empty) > ``ROCQ_WORKSPACE``.
    3. Timeout resolution.  *timeout_default* is the wrapper's
       compile/verify fallback (seconds): when set, the helper
       falls back to it for ``timeout<=0`` and does not clamp; when
       ``None`` the helper routes through :func:`_resolve_call_timeout`
       (the per-call cap that returns a ``clamped`` flag).
    4. ``_validate_workspace`` against the resolved workspace; on
       failure returns a :func:`_fail` envelope with the *already
       resolved* lifespan_state so the failure lands in
       ``recent_errors``.
    5. ``_maybe_workspace_warning`` against the resolved workspace.

    Returns either a failure envelope (caller returns it verbatim) or
    the tuple ``(workspace, lifespan_state, ws_warning, clamped,
    effective_timeout)``.
    """
    if ctx is None:
        if not ctx_optional:
            return _no_ctx_fail(tool)
        lifespan_state: dict[str, Any] | None = None
    else:
        lifespan_state = ctx.lifespan_context

    explicit_workspace = bool(workspace)
    if file:
        workspace = workspace or _find_project_root_from_file(file) or _config.ROCQ_WORKSPACE
    else:
        workspace = workspace or _config.ROCQ_WORKSPACE

    if timeout_default is not None:
        effective_timeout = (
            float(timeout)
            if timeout is not None and timeout > 0
            else float(timeout_default)
        )
        clamped = False
    else:
        effective_timeout, clamped = _resolve_call_timeout(timeout)

    err = _validate_workspace(workspace)
    if err:
        return _fail(lifespan_state, tool, err, "validation")

    ws_warning = _maybe_workspace_warning(
        workspace, explicit=explicit_workspace, file_provided=bool(file)
    )
    return workspace, lifespan_state, ws_warning, clamped, effective_timeout


def _finalize_tool_envelope(
    result: Any, *, clamped: bool, ws_warning: str | None
) -> Any:
    """Merge trailing envelope keys onto *result*.

    Mirrors the trailing 3-line block of every wrapper:

    - ``clamped_timeout``: echoes the cap value when the per-call
      timeout was clamped by :func:`_resolve_call_timeout`.
    - ``workspace_warning``: the advisory from
      :func:`_maybe_workspace_warning`, when set.

    Both merges are no-ops if *result* is not a ``dict`` so an
    implementation that returns a non-dict (unexpected) passes through
    untouched.
    """
    if not isinstance(result, dict):
        return result
    if clamped:
        result["clamped_timeout"] = _config.ROCQ_QUERY_TIMEOUT_CAP
    if ws_warning:
        result["workspace_warning"] = ws_warning
    return result

