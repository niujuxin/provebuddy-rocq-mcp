"""Thin MCP tool wrappers - interactive sessions (verbatim from upstream server.py)."""

from __future__ import annotations

from typing import Any

from fastmcp import Context

from core.config import ROCQ_QUERY_TIMEOUT_CAP
from core.envelope import (
    _finalize_tool_envelope,
    _no_ctx_fail,
    _resolve_tool_envelope,
)
from core.interactive import run_check, run_start, run_step_multi
from core.workspace import _resolve_call_timeout


async def rocq_start(
    file: str = "",
    theorem: str = "",
    workspace: str = "",
    line: int | None = None,
    character: int | None = None,
    preamble: str = "",
    force_restart: bool = False,
    timeout: int = 0,
    ctx: Context = None,
) -> dict[str, Any]:
    """Start an interactive proof session — see goals, explore tactics.

    Returns a state_id for use with rocq_check and rocq_step_multi.
    Also returns the current proof goals at the starting position,
    so this tool can be used to inspect goals at any point in a file.
    For a position inside a proof, the response also carries
    ``focus_depth`` — how many ``{...}`` / bullet focus frames are open
    above the goal (0 at the top level) — so a session resumed mid-proof
    knows its bullet nesting (omitted for preamble-only starts).

    Three start modes (precedence: theorem > position > preamble):
    1. By theorem: file + theorem — start proving a specific theorem.
       Tip: for a scratch file under ``/tmp`` that needs the project's
       load path, keep the file name stable across iterations (e.g.
       ``/tmp/probe.v``) — Fleche caches per file path, so rotating
       probe names defeats the warmth.
    2. By position: file + line + character — jump to any position in
       a file and see the proof goals there.  Useful for inspecting
       proof state at a specific point, or recovering from an error
       position returned by rocq_compile.
    3. From imports: preamble — set up an import context only.
       **Preferred for scratch iteration** (no project files needed,
       preferred over ``coqc /tmp/foo.v``): call
       ``rocq_start(preamble='Require Import ...')`` once, then iterate
       with rocq_check / rocq_step_multi against the returned state_id.
       The import set is content-hashed and warm across iterations even
       if you change the lemma body (see
       ``interactive.py:_get_or_create_import_state``).

    **Position semantics (mode 2):** ``line`` and ``character`` are
    0-indexed.  Petanque resolves the cursor to a sentence boundary by
    *rounding forward* through the sentence that contains the cursor:

    - Cursor on any character of a sentence — its first letter, any
      character inside, or its terminating period — yields the state
      **after** that whole sentence has executed.
    - Cursor in the whitespace **before** a sentence's first
      non-whitespace character yields the state **before** that
      sentence (= after the previous sentence).
    - Cursor in the whitespace **after** a sentence's terminating
      period yields the state **after** that sentence.

    So to inspect goals **before** a tactic, point at the whitespace
    just before its first character.  To inspect goals **after** a
    tactic, point at any character of the tactic (including its
    period) or at the whitespace immediately following the period.

    **Important:** The interactive session reads the file at start time and
    does not track subsequent edits. If another process or agent modifies the
    file while a session is active, the proof state becomes stale and tactics
    may fail or produce wrong results. To avoid this, work on a **copy** of
    the file for interactive proving, or restart the session after edits.

    Args:
        file: Path to the .v file (relative to workspace).
        theorem: Name of the theorem to prove.
        workspace: Workspace directory.  If omitted, auto-detected by walking
            up from *file* looking for ``_RocqProject`` / ``_CoqProject`` /
            ``dune-project``; falls back to the ``ROCQ_WORKSPACE`` env var
            (default: cwd).
        line: 0-based line number for position-based start.  See
            "Position semantics" above for how the cursor is resolved
            to a sentence boundary.
        character: 0-based character offset for position-based start.
            See "Position semantics" above.
        preamble: Import commands for preamble mode (e.g., "Require Import Lia.").
        force_restart: If True, kill pet, clear the state table, and
            respawn before starting.  Recovery primitive for the rare
            cases where the shared pet is in a bad state: accumulated
            RAM bloat after long shared use, indexing corruption, or a
            "State N expired" that repeats after a plain ``rocq_start``
            retry (suggesting a peer caller is also force-restarting).
            Actively-used states survive peer churn via LRU eviction —
            ``force_restart=True`` is *not* needed as routine insurance
            and is unhelpful when a recent response already carried
            ``pet_restarted: True`` (pet is already fresh).  See README
            "Concurrency model".  Default: False.
        timeout: Per-call timeout in seconds for opening the session.
            Default 0 uses ``ROCQ_PET_TIMEOUT`` (env var, default 30).
            Raise this for files with heavy library imports.
            Clamped to ``ROCQ_QUERY_TIMEOUT_CAP`` (default 300s) so a stray
            large value cannot park the pet lock indefinitely; when
            clamping fires the response includes ``clamped_timeout:
            <cap>`` so the caller can diagnose unexpected timeouts.

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
        tool="rocq_start", ctx=ctx, workspace=workspace, file=file, timeout=timeout
    )
    if not isinstance(resolved, tuple):
        return resolved
    workspace, lifespan_state, ws_warning, clamped, effective_timeout = resolved

    result = await run_start(
        file=file,
        theorem=theorem,
        workspace=workspace,
        lifespan_state=lifespan_state,
        line=line,
        character=character,
        preamble=preamble,
        force_restart=force_restart,
        timeout=effective_timeout,
    )
    return _finalize_tool_envelope(result, clamped=clamped, ws_warning=ws_warning)

async def rocq_step_multi(
    tactics: list[str],
    from_state: int,
    include_warnings: bool = True,
    timeout: int = 0,
    ctx: Context = None,
) -> dict[str, Any]:
    """Try multiple tactics at once — find what works without guessing.

    Tests each tactic against a specific proof state and returns all
    results. Does NOT advance the state — commit the winner with
    rocq_check.

    Use this whenever you're unsure which tactic to apply:
      tactics=["auto.", "lia.", "lra.", "ring.", "tauto.", "firstorder."]

    Or to auto-solve a subgoal, try the standard automation battery:
      tactics=["trivial.", "reflexivity.", "assumption.", "exact I.",
               "auto.", "eauto.", "tauto.", "intuition.", "lia.", "lra.",
               "nia.", "nra.", "ring.", "field.", "decide equality.",
               "firstorder."]
    Note: lia/lra/ring/field require the .v file to import Lia/Lra/Ring/Field.

    Or to explore proof structure:
      tactics=["destruct n.", "induction n.", "case_eq n."]

    Each result entry includes a ``feedback`` field (truncated string)
    when the tactic produces visible output (e.g., ``Print``, ``Search``).
    Each *successful* entry also carries a ``focus_depth`` field — how
    many ``{...}`` / bullet focus frames that tactic leaves open above
    the goal (0 at the top level).

    **Canonical exploration pattern:** if the first few steps of a proof
    are a confident prefix, advance with ``rocq_check`` first and pass
    the resulting ``state_id`` as ``from_state`` here — don't repeat the
    prefix inside every entry of ``tactics``.  See README
    "Recommended usage patterns → Multi-tactic exploration".

    Args:
        tactics: List of tactics to try (max 20).
        from_state: State ID to try the tactics from.  Required — use the
            ``state_id`` returned by ``rocq_start`` or a previous
            ``rocq_check``.  There is no implicit "current state"
            fallback (avoids cross-agent state confusion when peers
            share this rocq-mcp process).
        include_warnings: If True (default), per-tactic ``feedback`` includes
            all severities.  If False, drop entries at LSP Warning severity.
        timeout: Per-call timeout in seconds for the whole batch.  The
            per-tactic budget is ``timeout / len(tactics)`` (subject to the
            usual ``Timeout`` eligibility rules).  Default 0 uses
            ``ROCQ_PET_TIMEOUT`` (env var, default 30).  Raise this when
            individual tactics in the batch are expensive.  Clamped to
            ``ROCQ_QUERY_TIMEOUT_CAP``
            (default 300s) so a stray large value cannot park the pet
            lock indefinitely; when clamping fires the response includes
            ``clamped_timeout: <cap>`` so the caller can diagnose
            unexpected timeouts.

    On ``pet_restarted: True``, call ``rocq_diag`` for memory headroom and
    recent error history.
    """
    if ctx is None:
        return _no_ctx_fail("rocq_step_multi")

    effective_timeout, clamped = _resolve_call_timeout(timeout)

    result = await run_step_multi(
        tactics=tactics,
        lifespan_state=ctx.lifespan_context,
        from_state=from_state,
        include_warnings=include_warnings,
        timeout=effective_timeout,
    )
    if clamped:
        result["clamped_timeout"] = ROCQ_QUERY_TIMEOUT_CAP
    return result

async def rocq_check(
    body: str,
    from_state: int,
    workspace: str = "",
    timeout: int = 0,
    include_warnings: bool = True,
    ctx: Context = None,
) -> dict[str, Any]:
    """Run proof commands from cached imports — fast iterative checking.

    Much faster than rocq_compile for iterative proof development:
    imports are cached (first call processes them, subsequent calls skip), and on error
    returns the last valid state for immediate interactive recovery
    via rocq_check(from_state=...) or rocq_step_multi(from_state=...).

    When proof_finished=True, also returns proof_tactics (ordered list of
    all tactics from root to current state) and proof_hint (instructions
    for assembling the final .v file).  On a broken walk (an ancestor
    state was LRU-evicted, or a cycle was detected), proof_tactics and
    proof_hint are omitted; the response carries ``proof_tactics_status``
    (``"ancestor_evicted"`` or ``"cycle"``), ``proof_tactics_broken_at``
    (the state id where the walk gave up), and ``proof_tactics_hint``
    instead — clients that ignore these keys see no half-chain at all.

    Recommended workflow (each step threads ``state_id`` explicitly):
    1. ``s0 = rocq_start(file=..., theorem=...)["state_id"]``
    2. ``s1 = rocq_check(from_state=s0, body="intros. simpl.")["state_id"]``
    3. If stuck: ``rocq_step_multi(from_state=s1, tactics=[...])`` to explore
    4. ``rocq_check(from_state=s1, body="winning_tactic.")`` to commit

    When commands produce visible output (e.g., ``Print``, ``Check``,
    ``vm_compute``, ``native_compute``), a ``feedback`` field is included
    as a list of ``[command, output]`` pairs (truncated per step at 50K
    chars).  Omitted when no command produces output.

    When proof goals are available, the success response carries
    ``focus_depth`` — how many ``{...}`` / bullet focus frames are
    currently open above the goal (0 at the top level) — so an agent
    stepping through nested subgoals can tell how deep its focus nesting
    is.  (Omitted on empty-body checks and when goal state cannot be
    retrieved, like the other goal-derived fields.)

    **Note:** A held ``state_id`` freezes its Rocq environment at session
    start.  A ``stale_warning`` field is returned when that environment may
    no longer match disk: either the underlying ``.v`` file was modified
    after ``rocq_start``, or a dependency ``.vo`` in the workspace was
    rebuilt through this server since the session began (in which case
    ``proof_finished`` can diverge from a clean compile — re-verify with
    ``rocq_compile_file``).  Restart with ``rocq_start`` for a fresh session.

    Args:
        body: Commands to execute (one or more Rocq sentences).
        from_state: State ID to execute from.  Required — use the
            ``state_id`` returned by ``rocq_start`` or a previous
            ``rocq_check``.  There is no implicit "current state"
            fallback (avoids cross-agent state confusion when peers
            share this rocq-mcp process).
        workspace: Accepted for API compatibility but unused; the
            active workspace comes from the state entry set by
            ``rocq_start``.
        timeout: Per-call timeout in seconds for the batch of commands.
            Default 0 uses ``ROCQ_PET_TIMEOUT`` (env var, default 30).
            Raise this for compute-heavy tactics (``vm_compute``,
            ``native_compute``).  Clamped to ``ROCQ_QUERY_TIMEOUT_CAP``
            (default 300s) so a stray large value cannot park the pet
            lock indefinitely; when clamping fires the response includes
            ``clamped_timeout: <cap>`` so the caller can diagnose
            unexpected timeouts.
        include_warnings: If True (default), per-step ``feedback`` includes
            all severities.  If False, drop entries at LSP Warning severity.

    On ``pet_restarted: True``, call ``rocq_diag`` for memory headroom and
    recent error history.
    """
    # Note: workspace param is accepted for API compatibility but unused;
    # the active workspace comes from the state entry set by rocq_start.
    effective_timeout, clamped = _resolve_call_timeout(timeout)

    if ctx is None:
        return _no_ctx_fail("rocq_check")

    result = await run_check(
        body=body,
        lifespan_state=ctx.lifespan_context,
        from_state=from_state,
        timeout=effective_timeout,
        include_warnings=include_warnings,
    )
    if clamped and isinstance(result, dict):
        result["clamped_timeout"] = ROCQ_QUERY_TIMEOUT_CAP
    return result
