"""Thin MCP tool wrappers - compile / verify (verbatim from upstream server.py)."""

from __future__ import annotations

from typing import Any

from fastmcp import Context

from core.compile import run_verify
from core.compile_enrichment import (
    run_compile_file_with_state,
    run_compile_with_state,
)
from core.config import ROCQ_COQC_TIMEOUT, ROCQ_VERIFY_TIMEOUT
from core.envelope import (
    _finalize_tool_envelope,
    _record_error,
    _resolve_tool_envelope,
)


async def rocq_compile(
    source: str,
    workspace: str = "",
    timeout: int = 0,
    include_warnings: bool = True,
    ctx: Context = None,
) -> dict[str, Any]:
    """Compile a finished .v file via coqc.

    For *scratch iteration* on a single proof, prefer the interactive tools:

    - ``rocq_start`` opens a held session (imports stay warm across
      attempts).
    - ``rocq_check`` runs a candidate proof body against a held state.
    - ``rocq_step_multi`` tries several tactics at once against a held
      state and reports which succeeded.

    Use ``rocq_compile`` for finished proofs, axiom audits, and final
    verification — coqc reloads all imports per call (often several
    seconds on heavy library imports).

    On failure, the result includes ``error_positions`` and a ``hint``.
    When coq-lsp is available in the active MCP session, the result
    also includes ``state_capture_status``:

      - ``"ok"``: proof state was captured at the error position; the
        result also includes ``state_id``, ``goals``, ``file``,
        ``theorem``, and ``proof_finished``.  Recover via
        ``rocq_check(from_state=state_id)`` or
        ``rocq_step_multi(from_state=state_id)``.
      - ``"outside_proof"``: error is outside any open proof; no
        ``state_id`` is returned.  Follow the original ``hint``.
      - ``"timeout"`` / ``"crashed"`` / ``"lock_contended"`` /
        ``"unavailable"`` / ``"memory_exhausted"`` /
        ``"no_position"``: enrichment did not
        succeed; follow the original ``hint`` (typically
        ``rocq_start(file=..., line=..., character=...)``).

    Args:
        source: Complete Rocq (.v) file content to compile.
        workspace: Directory to use as workspace (default: ROCQ_WORKSPACE env var).
        timeout: Compilation timeout in seconds (default: ROCQ_COQC_TIMEOUT env var).
        include_warnings: If True (default), include deduplicated warnings
            before the error in the output.  Set to False to get only the
            error diagnostic, which keeps context compact.

    On ``pet_restarted: True`` (state-capture path crashed pet), call
    ``rocq_diag`` for memory headroom and recent error history.
    """
    resolved = _resolve_tool_envelope(
        tool="rocq_compile",
        ctx=ctx,
        workspace=workspace,
        timeout=timeout,
        timeout_default=ROCQ_COQC_TIMEOUT,
        ctx_optional=True,
    )
    if not isinstance(resolved, tuple):
        return resolved
    workspace, lifespan_state, ws_warning, clamped, effective_timeout = resolved

    result = await run_compile_with_state(
        source=source,
        workspace=workspace,
        timeout=effective_timeout,
        include_warnings=include_warnings,
        lifespan_state=lifespan_state,
    )
    return _finalize_tool_envelope(result, clamped=clamped, ws_warning=ws_warning)

async def rocq_compile_file(
    file: str,
    workspace: str = "",
    timeout: int = 0,
    include_warnings: bool = True,
    keep_vo: bool = False,
    mode: str = "full",
    timing: bool = False,
    ctx: Context = None,
) -> dict[str, Any]:
    """Compile a finished .v file on disk via coqc.

    For *scratch iteration* on a single proof, prefer the interactive tools:

    - ``rocq_start`` opens a held session (imports stay warm across
      attempts).
    - ``rocq_check`` runs a candidate proof body against a held state.
    - ``rocq_step_multi`` tries several tactics at once against a held
      state and reports which succeeded.

    Use ``rocq_compile_file`` for whole-file verification, axiom audits,
    and final compile — coqc reloads all imports per call (often several
    seconds on heavy library imports).  Preferred over ``rocq_compile``
    for large files because the source stays on disk (avoids transmitting
    the full text through the MCP transport).

    On failure, the result includes ``error_positions`` and a ``hint``.
    When coq-lsp is available in the active MCP session, the result
    also includes ``state_capture_status``:

      - ``"ok"``: proof state was captured at the error position; the
        result also includes ``state_id``, ``goals``, ``file``,
        ``theorem``, and ``proof_finished``.  Recover via
        ``rocq_check(from_state=state_id)`` or
        ``rocq_step_multi(from_state=state_id)``.
      - ``"outside_proof"``: error is outside any open proof; no
        ``state_id`` is returned.  Follow the original ``hint``.
      - ``"timeout"`` / ``"crashed"`` / ``"lock_contended"`` /
        ``"unavailable"`` / ``"memory_exhausted"`` /
        ``"no_position"``: enrichment did not
        succeed; follow the original ``hint`` (typically
        ``rocq_start(file=..., line=..., character=...)``).

    On a ``compile_error`` failure with coq-lsp available, the result
    may also include ``errors``: a list of per-proof errors discovered
    by walking the file through pet (one entry per failing chunk).
    Each entry is ``{proof_name, kind, start_line, end_line, code,
    message}``.  Complements ``error_positions`` — the latter is
    coqc's raw parse of the first diagnostic, while ``errors`` is
    pet's structured walk of the whole file.  The walk stops at the
    first top-level / inter-chunk error (a broken ``Require`` /
    ``Import`` / ``Notation``) rather than emitting the downstream
    "reference/library not found" cascade it would otherwise poison every
    later declaration with; errors in independent named declarations still
    accumulate.
    The field may be
    *present and empty* (``errors: []``) when the walker ran but pet
    did not reproduce the coqc-reported failure — treat this as "no
    additional errors found" rather than "no errors at all."  Absent
    on success, when coq-lsp is unavailable, when the walker could
    not run, and when ``ROCQ_COMPILE_MULTI_ERROR_CAP=0`` (feature
    disabled).  Tune via ``ROCQ_COMPILE_MULTI_ERROR_CAP`` (default 20,
    max entries) and ``ROCQ_COMPILE_MULTI_ERROR_TIMEOUT`` (default
    5.0s, per-``pet.run`` budget inside the walker).

    **In a dune project** (a ``dune-project`` ancestor is found), the
    compiled ``.vo``/``.vos`` is written to dune's ``_build/default/…``
    instead of next to the source, so it never shadows a pre-built artifact
    in the source tree.  Full-mode files that are part of a
    stanza build via ``dune build``; scratch files and ``vos``/``timing``
    modes compile with coqc redirected (``-o``) into the same
    ``_build/default`` path.  In this mode the ``.vo`` is always retained in
    ``_build`` (usable by sibling ``Require``s once the workspace's load
    path points there — see the dune load-path note under *Prerequisites*),
    so ``keep_vo`` has no effect.  When ``dune build`` cannot build a file
    (it is not part of a stanza), the response carries a
    ``dune_build_warning`` string noting that coqc was used instead.  Set
    ``ROCQ_DUNE_BUILD=0`` to force the legacy coqc-into-source-tree behavior.

    Outside a dune project, compilation artifacts
    (``.vo``/``.vok``/``.vos``/``.glob``/``.aux``) are cleaned up by
    default and the source file is preserved.  Set ``keep_vo=True`` to
    retain the compiled-artifact family (``.vo``/``.vok``/``.vos``) while
    still cleaning the diagnostic artifacts
    (``.glob``/``.aux``/``.vio``/``.timing``/``.coqaux``).  Typical use:
    compiling a file whose ``.vo`` will be imported by a sibling ``.v`` in
    the same workspace, or incremental compile loops that want to avoid
    rebuilding unchanged dependencies.

    When the call rewrites ``.vo`` files in a workspace that has active
    interactive sessions, the result also includes ``vo_rebuild_warning``:
    a soft advisory naming the workspace and the count of potentially
    affected sessions, with a hint to call ``rocq_start`` again to refresh
    held dependency state.  Quiet when no ``.vo`` changed, when no
    interactive session in this workspace exists, or when the workspace
    exceeds ``_VO_SCAN_FILE_CAP`` (.vo paths).  Setting ``keep_vo=True``
    makes this warning *more likely to fire* on subsequent
    ``rocq_compile_file`` calls in the same workspace: the produced
    ``.vo`` now persists between calls, so any later compile that
    rewrites it is observable as a fresh mtime delta.

    Args:
        file: Path to the .v file (relative to workspace).
        workspace: Workspace directory.  If omitted, auto-detected by walking
            up from *file* looking for ``_RocqProject`` / ``_CoqProject`` /
            ``dune-project``; falls back to the ``ROCQ_WORKSPACE`` env var
            (default: cwd).
        timeout: Compilation timeout in seconds (default: ROCQ_COQC_TIMEOUT env var).
        include_warnings: If True (default), include deduplicated warnings
            before the error in the output.  Set to False to get only the
            error diagnostic, which keeps context compact.
        keep_vo: If True, preserve the ``.vo``/``.vok``/``.vos`` outputs
            after coqc returns (diagnostic artifacts are still cleaned).
            Default False matches today's "clean everything but the
            source" behavior.  Useful when a sibling file in the same
            workspace will ``Require Import`` the result.  **No-op in a
            dune project** — there the artifact always lives in
            ``_build/default`` (never the source tree), so nothing is
            cleaned regardless; see the dune paragraph above.  **Note**:
            combining ``keep_vo=True`` with ``mode="vos"`` produces
            only a ``.vos`` artifact; downstream files compiled in
            ``mode="full"`` will fail with ``"Unable to locate
            library ... (while searching for a .vos file)"`` — use
            ``mode="full" keep_vo=True`` when the sibling consumer
            expects a ``.vo``.
        mode: Which coqc pass to run.  ``"full"`` (default) is today's
            behavior — coqc fully elaborates every proof body.  ``"vos"``
            adds ``-vos`` so coqc *skips proof bodies entirely* — it
            does NOT execute them.  ``"vos"`` is fast and catches
            missing imports, statement type errors, holes left in
            statements, and notation conflicts.  It does NOT validate
            proofs: a ``Theorem t : False. Proof. exact I. Qed.``
            passes under ``"vos"``.  Use it as a cheap pre-pass during
            iteration, then run ``"full"`` for the real check.
            ``"vos"`` produces a ``.vos`` artifact rather than a ``.vo``.
        timing: If True, invoke coqc with ``-time`` and attach a
            ``timing`` field to the response with per-sentence
            diagnostics — ``{"total_sentences": int, "top_slowest":
            list[{line, characters, name, duration_seconds}],
            "last_completed": {...} | None}``.  ``top_slowest`` holds
            up to 5 entries sorted by descending duration.  On
            timeout, ``last_completed`` is the final sentence coqc
            finished and the ``error`` string names it so "timed out
            after 590s" becomes "Last completed sentence: line 221
            [Theorem.foo] (15.3s)."  On a successful compile,
            ``last_completed`` is the file's literal final sentence
            (not a failure marker).  Default False is zero-overhead.

    The response envelope additionally carries several optional fields
    depending on flags / failure mode: ``error_positions`` and
    ``state_capture_status`` on ``reason="compile_error"`` (see the
    ``state_capture_status`` paragraph above) — **except** when a dune build
    fails inside a *dependency* rather than the requested file: that case
    still returns ``reason="compile_error"`` but omits ``error_positions``
    (they carry no filename and would point into the wrong file) and instead
    carries a ``hint`` naming the dependency to fix first; read the file name
    from the ``error`` text.  Also: ``errors`` per-declaration
    list when ``pet`` is available (see the Multi-error callout in the
    README); ``vo_rebuild_warning`` when the call rewrites ``.vo``
    artifacts in a workspace with active sessions; ``dune_build_warning``
    when a dune project file could not be built by ``dune build`` and coqc
    was used instead (see the dune paragraph above); ``clamped_timeout``
    when the per-call timeout was clamped by ``ROCQ_QUERY_TIMEOUT_CAP``;
    ``timing`` when ``timing=True``.

    On ``pet_restarted: True`` (state-capture path crashed pet), call
    ``rocq_diag`` for memory headroom and recent error history.
    """
    resolved = _resolve_tool_envelope(
        tool="rocq_compile_file",
        ctx=ctx,
        workspace=workspace,
        file=file,
        timeout=timeout,
        timeout_default=ROCQ_COQC_TIMEOUT,
        ctx_optional=True,
    )
    if not isinstance(resolved, tuple):
        return resolved
    workspace, lifespan_state, ws_warning, clamped, effective_timeout = resolved

    result = await run_compile_file_with_state(
        file=file,
        workspace=workspace,
        timeout=effective_timeout,
        include_warnings=include_warnings,
        lifespan_state=lifespan_state,
        keep_vo=keep_vo,
        mode=mode,
        timing=timing,
    )
    return _finalize_tool_envelope(result, clamped=clamped, ws_warning=ws_warning)

async def rocq_verify(
    proof: str,
    problem_name: str,
    problem_statement: str,
    workspace: str = "",
    timeout: int = 0,
    include_warnings: bool = True,
    ctx: Context = None,
) -> dict[str, Any]:
    """Verify that a proof actually proves the original statement.

    Wraps the proof in a Module M sandbox and checks that the theorem
    matches the original problem_statement. Catches type redefinition,
    Admitted/Abort, custom axioms, and statement mismatches. Standard
    mathematical axioms (classical logic, Reals, etc.) are accepted.

    Run this after rocq_compile succeeds to confirm correctness.

    Args:
        proof: The complete proof file content (including imports).
        problem_name: The unqualified theorem name (e.g., "add_comm", not "Nat.add_comm").
        problem_statement: The original problem file content (with Admitted/Abort).
        workspace: Directory to use as workspace (default: ROCQ_WORKSPACE env var).
        timeout: Verification timeout in seconds (default: ROCQ_VERIFY_TIMEOUT env var).
        include_warnings: If True (default), include deduplicated warnings
            before the error in the output.  Set to False for compact errors.

    Returns the unified envelope ``{success, error, reason, ...}``.
    On failure, ``reason`` is one of:
        - ``"validation"``: invalid identifier, oversize source, malformed input.
        - ``"compile_error"``: the proof failed to compile.
        - ``"axiom_dependency"``: the proof relies on Admitted, ``admit``, or
          a custom (non-standard) axiom.
        - ``"type_mismatch"``: Phase 3 found that the proof's type differs
          from the problem's type.
        - ``"timeout"``: verification exceeded the budget across all phases.

    On success, ``assumptions`` and ``verification_method`` describe how
    the verdict was reached (``module_m``, ``shared_defs``, ``direct``).

    On ``pet_restarted: True`` (Phase 2 ``rocq_query`` path crashed pet
    while extracting shared definitions), call ``rocq_diag`` for memory
    headroom and recent error history.
    """
    resolved = _resolve_tool_envelope(
        tool="rocq_verify",
        ctx=ctx,
        workspace=workspace,
        timeout=timeout,
        timeout_default=ROCQ_VERIFY_TIMEOUT,
        ctx_optional=True,
    )
    if not isinstance(resolved, tuple):
        return resolved
    workspace, lifespan_state, ws_warning, clamped, effective_timeout = resolved

    result = await run_verify(
        proof=proof,
        problem_name=problem_name,
        problem_statement=problem_statement,
        workspace=workspace,
        timeout=effective_timeout,
        include_warnings=include_warnings,
        lifespan_state=lifespan_state,
    )
    # Record verification failures (success=False with an error message)
    # so rocq_diag surfaces them.  Pet-level crashes routed through
    # run_verify -> _run_with_pet (Phase 2 toc lookup) are already
    # recorded inside that helper, so skip when ``pet_restarted=True``
    # to avoid the double-record bug — the prior entry already carries
    # tool="rocq_verify" with the right reason because _extract_problem_structure
    # passes that tool name to _run_with_pet.
    if (
        isinstance(result, dict)
        and result.get("success") is False
        and result.get("error")
        and not result.get("pet_restarted")
    ):
        _record_error(
            lifespan_state,
            "rocq_verify",
            str(result["error"]),
            reason=str(result.get("reason") or "validation"),
        )
    return _finalize_tool_envelope(result, clamped=clamped, ws_warning=ws_warning)
