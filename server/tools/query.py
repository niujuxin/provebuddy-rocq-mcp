"""Thin MCP tool wrappers - query / explore (verbatim from upstream server.py)."""

from __future__ import annotations

from typing import Any

from fastmcp import Context

from core.envelope import _finalize_tool_envelope, _resolve_tool_envelope
from core.interactive import (
    run_assumptions,
    run_notations,
    run_query,
    run_toc,
)


async def rocq_query(
    command: str,
    preamble: str = "",
    file: str = "",
    workspace: str = "",
    max_results: int | None = None,
    include_warnings: bool = True,
    timeout: int = 0,
    from_state: int | None = None,
    ctx: Context = None,
) -> dict[str, Any]:
    """Search the Rocq environment — find lemmas, check types, inspect definitions.

    Does NOT modify any proof state. Use this to explore before proving:
      command="Search (nat -> nat -> nat)."  — find relevant lemmas
      command="Check Nat.add."               — check a term's type
      command="Print Nat.add."               — see a definition
      command="About plus."                  — summary of a name

    Three context modes (mutually exclusive in practice):
    - **preamble mode** (default): pass import / scope commands as a
      string.  Scope and import statements like ``Require Import``,
      ``From X Require Y``, ``Open Scope``, ``Set``, ``Unset``,
      ``Local``, and ``Section`` belong here — NOT inside ``command=``.
      ``command=`` runs each statement in isolation, so e.g. an
      ``Open Scope`` placed in ``command`` would not propagate to a
      following ``Search``.  See README "Recommended usage patterns →
      Imports and scopes in rocq_query".
    - **file mode**: pass a ``.v`` file path; the query runs with all
      definitions from that file in scope.  More reliable than preamble
      because it captures ``Open Scope``, ``Set`` options, etc., in the
      exact order the file declares them.
    - **from_state mode**: pass a ``state_id`` from a live ``rocq_check``
      session to query against the live proof context — opened scopes,
      hypotheses, and local definitions are all visible to ``Search`` /
      ``Print`` / ``About`` / ``Locate``.  The query runs against a
      transient child state which is discarded; the parent state is
      unchanged.  Canonical pattern::

          state_id = (await rocq_check(body=..., from_state=...))["state_id"]
          await rocq_query(command="Search _.", from_state=state_id)

      Prefer this over ``rocq_check(from_state=N, body="Search ...")``
      for pure queries — no new ``state_id`` is allocated and the
      state-table is not polluted.

    Args:
        command: The Rocq query command to execute.
        preamble: Optional import lines needed for the query context
                  (e.g., "Require Import Reals.\\nOpen Scope R_scope.").
        file: Path to a .v file (relative to workspace) whose definitions
            should be in scope. Mutually exclusive with preamble and
            from_state.
        workspace: Workspace directory.  If omitted, auto-detected by walking
            up from *file* looking for ``_RocqProject`` / ``_CoqProject`` /
            ``dune-project``; falls back to the ``ROCQ_WORKSPACE`` env var
            (default: cwd).
        max_results: Optional maximum number of results to return.
            Useful for broad Search patterns. If omitted, all results are
            returned (subject to character limit).
        include_warnings: If True (default), include all feedback returned
            by the query.  If False, drop entries at LSP Warning severity
            so warning noise does not crowd out tool output.
        timeout: Per-call timeout in seconds for expensive computations
            like ``Time Eval vm_compute in ...``.  ``0`` (default) means
            use ``ROCQ_PET_TIMEOUT``.  Clamped to ``ROCQ_QUERY_TIMEOUT_CAP``
            (default 300s); when clamping fires the response includes
            ``clamped_timeout: <cap>`` so the caller can diagnose unexpected
            timeouts.
        from_state: A live state_id (from ``rocq_start`` / ``rocq_check`` /
            ``rocq_step_multi``) to query against.  Mutually exclusive with
            *file*.  When set, *preamble* is ignored.

    On ``pet_restarted: True``, call ``rocq_diag`` for memory headroom and
    recent error history.
    """
    resolved = _resolve_tool_envelope(
        tool="rocq_query", ctx=ctx, workspace=workspace, file=file, timeout=timeout
    )
    if not isinstance(resolved, tuple):
        return resolved
    workspace, lifespan_state, ws_warning, clamped, effective_timeout = resolved

    result = await run_query(
        command=command,
        preamble=preamble,
        workspace=workspace,
        lifespan_state=lifespan_state,
        file=file,
        max_results=max_results,
        include_warnings=include_warnings,
        timeout=effective_timeout,
        from_state=from_state,
    )
    return _finalize_tool_envelope(result, clamped=clamped, ws_warning=ws_warning)

async def rocq_assumptions(
    name: str,
    file: str,
    workspace: str = "",
    timeout: int = 0,
    ctx: Context = None,
) -> dict[str, Any]:
    """List the axioms a theorem depends on.

    Runs ``Print Assumptions`` on the given theorem/lemma name and returns
    the resulting assumption list verbatim.  No classification is performed
    — this tool is pure introspection; the agent decides what's safe to
    trust.  Use ``rocq_verify`` for an admit-free / sandboxed trust
    decision on a candidate proof.

    The theorem must be defined in the given file.  The tool reads the file
    to set up the full Rocq environment (imports, scopes, definitions),
    ensuring the correct theorem is resolved even when names are reused
    across sections.

    Args:
        name: The theorem/lemma name to check (e.g., "add_comm").
        file: Path to the .v file where the theorem is defined (relative to workspace).
        workspace: Workspace directory.  If omitted, auto-detected by walking
            up from *file* looking for ``_RocqProject`` / ``_CoqProject`` /
            ``dune-project``; falls back to the ``ROCQ_WORKSPACE`` env var
            (default: cwd).
        timeout: Per-call timeout in seconds for the ``Print Assumptions``
            query.  Default 0 uses ``ROCQ_PET_TIMEOUT`` (env var, default
            30).  Raise this when the theorem's opaque-proof fetch from
            ``.vo`` files is slow.  Clamped to ``ROCQ_QUERY_TIMEOUT_CAP``
            (default 300s) so a stray large value cannot park the pet
            lock indefinitely; when clamping fires the response includes
            ``clamped_timeout: <cap>`` so the caller can diagnose
            unexpected timeouts.

            **Tip:** ``Print Assumptions`` triggers ``.vo`` opaque-proof
            fetching on first call (often 40+ modules on heavy library
            imports).  A pet restart from a timeout wipes Fleche, so a
            retry with the *same* timeout pays the same opaque-fetch
            cost from scratch and will time out again — the cost
            survives ``pet_restarted: True``.  Set ``timeout=`` high on
            the *first* call rather than relying on a retry after
            restart.

    Returns (key fields):
        success:     bool.
        theorem:     the cleaned theorem name.
        assumptions: list[str] of ``"name : type"`` pairs from
                     ``Print Assumptions``.  Empty when the theorem is closed
                     under the global context.  ``Print Assumptions`` does
                     not distinguish ``Admitted`` from ``Axiom`` / ``Parameter``
                     / ``Conjecture``, so admits and user axioms appear here
                     side-by-side.
        raw_output:  full raw ``Print Assumptions`` output.

    On theorem-not-found errors: response includes ``available_in_file:
    list[str]`` with the file's defined names (sorted, capped — see
    ``available_in_file_limit`` in the response when truncated).  When the
    file has more names than the cap, ``available_in_file_truncated:
    true``, ``available_in_file_total: <int>`` (uncapped count), and
    ``available_in_file_limit: <int>`` (the active cap) are also
    included; call ``rocq_toc`` for the full list.  Agents can fuzzy-
    match the requested name against this list to recover from typos.

    On ``pet_restarted: True``, call ``rocq_diag`` for memory headroom and
    recent error history.
    """
    resolved = _resolve_tool_envelope(
        tool="rocq_assumptions",
        ctx=ctx,
        workspace=workspace,
        file=file,
        timeout=timeout,
    )
    if not isinstance(resolved, tuple):
        return resolved
    workspace, lifespan_state, ws_warning, clamped, effective_timeout = resolved

    result = await run_assumptions(
        name=name,
        file=file,
        workspace=workspace,
        lifespan_state=lifespan_state,
        timeout=effective_timeout,
    )
    return _finalize_tool_envelope(result, clamped=clamped, ws_warning=ws_warning)

async def rocq_toc(
    file: str,
    workspace: str = "",
    timeout: int = 0,
    ctx: Context = None,
) -> dict[str, Any]:
    """Get the structure of a Rocq file: all definitions, lemmas, theorems, and sections.

    Returns a hierarchical outline showing what is defined in the file.
    Useful for understanding a file before working with it, or finding
    the name of a theorem to prove.

    Does NOT require a rocq_start session.

    Args:
        file: Path to the .v file (relative to workspace).
        workspace: Workspace directory.  If omitted, auto-detected by walking
            up from *file* looking for ``_RocqProject`` / ``_CoqProject`` /
            ``dune-project``; falls back to the ``ROCQ_WORKSPACE`` env var
            (default: cwd).
        timeout: Per-call timeout in seconds for the ``pet.toc`` lookup.
            Default 0 uses ``ROCQ_PET_TIMEOUT`` (env var, default 30).
            Raise this for very large files with heavy library imports.
            Clamped to ``ROCQ_QUERY_TIMEOUT_CAP`` (default 300s) so a
            stray large value cannot park the pet lock indefinitely;
            when clamping fires the response includes ``clamped_timeout:
            <cap>`` so the caller can diagnose unexpected timeouts.

    On ``pet_restarted: True``, call ``rocq_diag`` for memory headroom and
    recent error history.
    """
    resolved = _resolve_tool_envelope(
        tool="rocq_toc", ctx=ctx, workspace=workspace, file=file, timeout=timeout
    )
    if not isinstance(resolved, tuple):
        return resolved
    workspace, lifespan_state, ws_warning, clamped, effective_timeout = resolved

    result = await run_toc(
        file=file,
        workspace=workspace,
        lifespan_state=lifespan_state,
        timeout=effective_timeout,
    )
    return _finalize_tool_envelope(result, clamped=clamped, ws_warning=ws_warning)

async def rocq_notations(
    statement: str,
    preamble: str = "",
    workspace: str = "",
    timeout: int = 0,
    ctx: Context = None,
) -> dict[str, Any]:
    """List all notations in a Rocq statement and how they resolve.

    Helps debug notation ambiguity (e.g., which scope does "+" resolve to?
    Is "=" Leibniz equality or Qeq?).

    Pass the statement part of a Lemma/Theorem declaration (after the colon).
    For example, for "Lemma foo : forall n, n + 0 = n", pass
    statement="forall n, n + 0 = n".

    NOTE: Only works on statements (propositions/types), not arbitrary terms.

    Args:
        statement: The proposition/type to analyze.
        preamble: Import lines for context (e.g., "Require Import QArith.").
        workspace: Workspace directory (default: ROCQ_WORKSPACE env var).
        timeout: Per-call timeout in seconds for the notation lookup.
            Default 0 uses ``ROCQ_PET_TIMEOUT`` (env var, default 30).
            Raise this for statements that require heavy library imports.
            Clamped to ``ROCQ_QUERY_TIMEOUT_CAP`` (default 300s) so a
            stray large value cannot park the pet lock indefinitely;
            when clamping fires the response includes ``clamped_timeout:
            <cap>`` so the caller can diagnose unexpected timeouts.

    On ``pet_restarted: True``, call ``rocq_diag`` for memory headroom and
    recent error history.
    """
    resolved = _resolve_tool_envelope(
        tool="rocq_notations", ctx=ctx, workspace=workspace, timeout=timeout
    )
    if not isinstance(resolved, tuple):
        return resolved
    workspace, lifespan_state, ws_warning, clamped, effective_timeout = resolved

    result = await run_notations(
        statement=statement,
        preamble=preamble,
        workspace=workspace,
        lifespan_state=lifespan_state,
        timeout=effective_timeout,
    )
    return _finalize_tool_envelope(result, clamped=clamped, ws_warning=ws_warning)
