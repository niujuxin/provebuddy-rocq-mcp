"""Core coqc execution and output parsing.

Split from upstream ``compile.py`` (rocq-mcp @ 6983113d0844c0b7f987c79dab13988445109bfb): subprocess runners,
error/timing parsing, result assembly, and Rocq sentence splitting.  Only
imports are re-pointed; function bodies are byte-identical to upstream.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from core import config as _config
from core import workspace as _workspace
from core.verify import _rocq_scan

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_ERROR_LENGTH: int = 4000
_MAX_FORMAT_WARNINGS: int = 3
_PROOF_FILE_LABEL: str = "<proof>"


# ---------------------------------------------------------------------------
# coqc runner
# ---------------------------------------------------------------------------


def _run_build_subprocess(
    args: list[str],
    cwd: str,
    timeout: int,
) -> dict[str, Any]:
    """Run *args* under *cwd* with graceful SIGTERM → SIGKILL timeout escalation.

    Shared by :func:`_run_coqc_process` (coqc) and :func:`_run_dune_build`
    (``dune build``) — both are long-running Rocq compiles that must not
    wedge the server on a diverging tactic.  On timeout the partial output
    buffers are still returned so the last completed sentence stays
    recoverable.

    Returns dict with keys ``returncode`` / ``stdout`` / ``stderr`` /
    ``timed_out``.  A missing / unexecutable ``args[0]`` yields
    ``returncode == -1`` with the reason in ``stderr``.
    """
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return {
                "returncode": proc.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "timed_out": False,
            }
        except subprocess.TimeoutExpired:
            # Graceful shutdown: SIGTERM first, escalate to SIGKILL
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except OSError:
                try:
                    proc.terminate()
                except OSError:
                    pass
            try:
                stdout, stderr = proc.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except OSError:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                try:
                    stdout, stderr = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    stdout, stderr = "", ""
            return {
                "returncode": -1,
                "stdout": stdout or "",
                "stderr": stderr or "",
                "timed_out": True,
            }
    except (FileNotFoundError, OSError) as e:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": (
                f"{args[0]} not found or not executable: {e}"
                if isinstance(e, FileNotFoundError)
                else f"Failed to run {args[0]}: {e}"
            ),
            "timed_out": False,
        }


def _run_coqc_process(
    file_path: str,
    workspace: Path,
    timeout: int,
    mode: str = "full",
    timing: bool = False,
    output: str | None = None,
) -> dict[str, Any]:
    """Run coqc on a .v file and return the result.

    Shared subprocess management for both :func:`_run_coqc` (temp files) and
    :func:`_run_coqc_file` (user files).  Handles timeout with graceful
    SIGTERM → SIGKILL escalation.

    When ``mode == "vos"`` passes ``-vos`` to coqc, which skips proof
    bodies (produces a ``.vos`` artifact instead of ``.vo``).

    When *timing* is True, coqc is invoked with ``-time`` so per-sentence
    timing diagnostics are emitted on stdout (coqc 9.x; older builds may
    have used stderr — :func:`_parse_timing_lines` tries stdout first and
    falls back to stderr).  On timeout the partial buffers are still
    returned so the last completed sentence remains recoverable.

    When *output* is given, coqc is passed ``-o <output>`` so the compiled
    ``.vo``/``.vos`` (and its sibling ``.glob``/``.aux``) land there rather
    than next to the source — used to target a dune ``_build/default`` path
    so the source tree stays free of ``.vo`` shadows.  coqc
    derives the module's logical name from the *output* path relative to the
    ``-R``/``-Q`` roots, so the caller must place *output* under the
    load-path root that maps to the file's logical prefix.

    Returns dict with keys:
        returncode: int
        stdout: str
        stderr: str
        timed_out: bool
    """
    coqc_args: list[str] = [
        _config.ROCQ_COQC_BINARY,
        *_workspace._parse_project_flags(workspace),
    ]
    if mode == "vos":
        coqc_args.append("-vos")
    if timing:
        coqc_args.append("-time")
    if output is not None:
        coqc_args += ["-o", output]
    coqc_args.append(file_path)
    return _run_build_subprocess(coqc_args, str(workspace), timeout)


def _run_coqc(source: str, workspace: str, timeout: int) -> dict[str, Any]:
    """Write source to temp file, run coqc, return result dict."""
    ws = Path(workspace).resolve()
    with tempfile.NamedTemporaryFile(
        suffix=".v", mode="w", delete=False, dir=str(ws)
    ) as f:
        f.write(source)
        f.flush()
        tmp_path = f.name

    try:
        return _run_coqc_process(tmp_path, ws, timeout)
    finally:
        _workspace._cleanup_coqc_artifacts(tmp_path)


_VO_FAMILY: tuple[str, ...] = (".vo", ".vok", ".vos")


def _run_coqc_file(
    file_path: str,
    workspace: str,
    timeout: int,
    keep_vo: bool = False,
    mode: str = "full",
    timing: bool = False,
    output: str | None = None,
) -> dict[str, Any]:
    """Run coqc on an existing .v file, return result dict.

    Unlike :func:`_run_coqc`, does NOT create a temp file — runs coqc
    directly on the given file.  Cleans up compilation artifacts but
    preserves the source .v file.  When *keep_vo* is True, also
    preserves the ``.vo``/``.vok``/``.vos`` compiled-artifact family
    so the produced ``.vo`` is available to sibling files importing
    it; the diagnostic artifacts (``.glob``/``.aux``/``.vio``/
    ``.timing``/``.coqaux``) are still cleaned.

    When ``mode == "vos"`` passes ``-vos`` to coqc, which checks
    statements / imports / notations but skips proof bodies.  Produces
    a ``.vos`` artifact rather than a ``.vo``.

    When *timing* is True, coqc is invoked with ``-time`` and its
    per-sentence timing lines land in the returned ``stderr`` for the
    caller to parse via :func:`_parse_timing_lines`.

    When *output* is given it is forwarded to coqc as ``-o`` (see
    :func:`_run_coqc_process`).  coqc then writes *every* artifact next to
    *output* (a dune ``_build/default`` path), so the source-tree cleanup
    below finds nothing to remove — the ``_build`` artifacts are left in
    place regardless of *keep_vo* (they belong to the build dir, and after
    ``dune``-style load-path resolution siblings import them from there).
    """
    ws = Path(workspace).resolve()
    try:
        return _run_coqc_process(
            file_path, ws, timeout, mode=mode, timing=timing, output=output
        )
    finally:
        base = Path(file_path).with_suffix("")
        for ext in _workspace._CLEANUP_EXTENSIONS:
            if ext == ".v":
                continue
            if keep_vo and ext in _VO_FAMILY:
                continue
            base.with_suffix(ext).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Error formatting
# ---------------------------------------------------------------------------

_COQC_POS_RE = re.compile(
    r'File "[^"]*", line (\d+), characters (\d+)-(\d+):\s*\n((?:Error|Warning):.*?)(?=File "|$)',
    re.DOTALL,
)


def _parse_coqc_error_positions(
    stderr: str,
    *,
    include_warnings: bool = True,
) -> list[dict[str, Any]]:
    """Parse coqc stderr into structured error positions.

    coqc uses 1-based lines, 0-based characters.
    Returns 0-based line numbers (for pytanque compatibility).

    The regex matches both ``Error:`` and ``Warning:`` diagnostics; when
    ``include_warnings=False``, ``Warning:`` entries are filtered out so
    callers don't surface warning bodies via this structured channel.
    """
    positions = []
    for m in _COQC_POS_RE.finditer(stderr):
        line_1based = int(m.group(1))
        char_start = int(m.group(2))
        char_end = int(m.group(3))
        message = m.group(4).strip()
        if not include_warnings and message.startswith("Warning:"):
            continue
        positions.append(
            {
                "line": line_1based - 1,
                "character": char_start,
                "end_character": char_end,
                "message": message[:500],
            }
        )
    return positions


def _first_error_from_positions(
    positions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return the first Error-level entry from parsed diagnostic positions."""
    for pos in positions:
        if pos["message"].startswith("Error:"):
            return pos
    return None


# Regex to match coqc diagnostic blocks: File "path", line N, characters S-E:\n<body>
_COQC_DIAG_RE = re.compile(
    r'(File "([^"]*)", line (\d+), characters (\d+)-(\d+):\s*\n)(.*?)(?=File "|$)',
    re.DOTALL,
)

# Regex to extract Error/Warning kind from body
_KIND_RE = re.compile(r"^(Error|Warning)\b")

# Regex to replace tmp file paths with <proof>
_TMP_PATH_RE = re.compile(r'"[^"]*tmp[^"]*\.v"')

_WARNING_PREFIX = "Warning:"


def _drop_warning_lines(text: str) -> str:
    """Drop lines that begin with `Warning:` (after leading whitespace).

    Used as the unstructured fallback when a coqc stderr block has no
    `File "..."` header to anchor structured `_format_error` parsing.
    """
    return "\n".join(
        line
        for line in text.splitlines()
        if not line.lstrip().startswith(_WARNING_PREFIX)
    ).strip()


def _format_error(
    error_str: str,
    proof_str: str,
    *,
    include_warnings: bool = True,
    file_label: str = _PROOF_FILE_LABEL,
) -> str:
    """Reformat a raw coqc stderr string into LLM-friendly feedback.

    - Replaces the opaque tmp file path with ``file_label``
    - Annotates the first Error-level diagnostic with the source line
      and a caret underline marking the exact character range
    - Suppresses pure-warning outputs (they don't prevent compilation)

    Args:
        error_str: Raw coqc stderr output.
        proof_str: The Rocq source that was compiled (for source annotations).
        include_warnings: If True (default), include deduplicated warnings
            that precede the first error.  If False, return only the error
            diagnostic itself — useful when warnings would drown the context.
        file_label: Label to use in error headers instead of the temp file
            path.  Defaults to ``"<proof>"``.

    Falls back to the raw string (path-cleaned) when no structured
    location info is present (timeouts, workspace errors, etc.).
    """
    if not error_str:
        return error_str

    proof_lines = proof_str.splitlines()
    diagnostics = list(_COQC_DIAG_RE.finditer(error_str))

    if not diagnostics:
        cleaned = _TMP_PATH_RE.sub(f'"{file_label}"', error_str).strip()
        if not include_warnings:
            cleaned = _drop_warning_lines(cleaned)
        # Cap output so unstructured errors don't drown LLM context
        if len(cleaned) > _MAX_ERROR_LENGTH:
            cleaned = cleaned[-_MAX_ERROR_LENGTH:]
        return cleaned

    parsed = []
    for m in diagnostics:
        kind_m = _KIND_RE.match(m.group(6).strip())
        parsed.append(
            {
                "kind": kind_m.group(1) if kind_m else "Error",
                "line": int(m.group(3)),
                "char_start": int(m.group(4)),
                "char_end": int(m.group(5)),
                "body": m.group(6).strip(),
            }
        )

    has_errors = any(d["kind"] == "Error" for d in parsed)
    if not has_errors:
        return ""

    # Select diagnostics to include in the output.
    # Deduplicate warnings by body text — coqc often emits the same
    # deprecation notice multiple times during elaboration.
    # Cap at _MAX_FORMAT_WARNINGS unique warnings to avoid drowning
    # LLM context (large projects can emit many unique warnings).
    selected = []
    seen_warnings: set[str] = set()
    for d in parsed:
        if d["kind"] == "Warning":
            if not include_warnings:
                continue
            if d["body"] in seen_warnings:
                continue
            if len(seen_warnings) >= _MAX_FORMAT_WARNINGS:
                continue
            seen_warnings.add(d["body"])
        selected.append(d)
        if d["kind"] == "Error":
            break

    parts = []
    for d in selected:
        line_1 = d["line"]
        char_start = d["char_start"]
        char_end = d["char_end"]

        header = f"{file_label}, line {line_1}, characters {char_start}-{char_end}:"

        line_idx = line_1 - 1
        source_line = (
            proof_lines[line_idx] if 0 <= line_idx < len(proof_lines) else None
        )

        annotation = ""
        if source_line is not None:
            prefix = f"  {line_1:4d} | "
            caret_offset = len(prefix) + char_start
            caret_len = max(1, char_end - char_start)
            annotation = (
                f"\n{prefix}{source_line}\n" f"{' ' * caret_offset}{'^' * caret_len}"
            )

        parts.append(f"{header}{annotation}\n{d['body']}")

    output = "\n\n".join(parts)
    if len(output) > _MAX_ERROR_LENGTH:
        output = output[-_MAX_ERROR_LENGTH:]
    return output


# ---------------------------------------------------------------------------
# Per-sentence timing diagnostics (coqc -time)
# ---------------------------------------------------------------------------

# Default number of slowest sentences to surface in the response.
_TIMING_TOP_N: int = 5

# coqc -time emits lines of the shape
#     Chars <start> - <end> [<vernac-name>] <duration> secs (<u>u,<s>s)
# where <duration> may be ``0.``, ``0.013``, etc.  The tail in
# parentheses is optional and varies across coqc versions, so the
# regex only anchors the prefix that all versions emit.
_TIMING_LINE_RE = re.compile(
    r"^Chars\s+(\d+)\s+-\s+(\d+)\s+\[([^\]]*)\]\s+([0-9.]+)\s+secs"
)


def _char_offset_to_line(source: str, offset: int) -> int:
    """Convert a 0-based character offset to a 1-based line number.

    O(N) per call in the size of *source* up to *offset*; acceptable
    because timing-line counts are bounded by source size.  Falls
    back to line 1 when *offset* is negative or out of range.
    """
    if offset <= 0 or not source:
        return 1
    capped = min(offset, len(source))
    return source.count("\n", 0, capped) + 1


def _parse_timing_lines(text: str, source: str) -> list[dict[str, Any]]:
    """Parse coqc ``-time`` lines from *text* into structured entries.

    coqc 9.x emits ``-time`` output on **stdout** (not stderr); earlier
    versions varied.  The parser is input-agnostic — it scans whatever
    text the caller hands it for ``Chars ... secs`` lines.

    Tolerant by design: any line that does not match
    :data:`_TIMING_LINE_RE` is ignored (warnings, errors, blanks all
    pass through silently).  When duration parsing fails the entry is
    dropped rather than poisoning the rest of the list — coqc emits
    ``0.`` and other unusual decimal shapes, so a permissive float
    cast with fallback keeps us robust to format drift.

    Each returned entry is a dict with keys ``line`` (1-based),
    ``characters`` (``[start, end]`` 0-based), ``name`` (vernac name
    as printed by coqc, with ``~`` left as-is — that is coqc's
    standard whitespace-substitute), and ``duration_seconds``.
    """
    entries: list[dict[str, Any]] = []
    if not text:
        return entries
    for raw_line in text.splitlines():
        m = _TIMING_LINE_RE.match(raw_line)
        if m is None:
            continue
        try:
            start = int(m.group(1))
            end = int(m.group(2))
            name = m.group(3)
            duration = float(m.group(4))
        except (TypeError, ValueError):
            continue
        entries.append(
            {
                "line": _char_offset_to_line(source, start),
                "characters": [start, end],
                "name": name,
                "duration_seconds": duration,
            }
        )
    return entries


def _strip_timing_lines(text: str) -> str:
    """Remove coqc ``-time`` lines from *text*.

    Used by :func:`_build_compile_result` when timing is enabled to
    keep the ``Chars ... secs`` noise out of the success-path
    ``output`` field (where they'd drown the rest of coqc's stdout).
    Non-timing lines are passed through unchanged.
    """
    if not text:
        return text
    return "\n".join(
        line for line in text.splitlines() if not _TIMING_LINE_RE.match(line)
    )


def _build_timing_field(
    timing_entries: list[dict[str, Any]],
    top_n: int = _TIMING_TOP_N,
) -> dict[str, Any]:
    """Assemble the ``timing`` response field from parsed entries.

    ``top_slowest`` is a stable-by-input-order sort by descending
    duration (Python sort is stable, so equal-duration entries keep
    source-position order).  ``last_completed`` is the final emitted
    entry — useful when coqc was killed mid-compile because it points
    at the sentence whose work was lost.
    """
    total = len(timing_entries)
    sorted_by_dur = sorted(
        timing_entries,
        key=lambda e: e["duration_seconds"],
        reverse=True,
    )
    top_slowest = sorted_by_dur[: max(0, top_n)]
    last_completed = timing_entries[-1] if timing_entries else None
    return {
        "total_sentences": total,
        "top_slowest": top_slowest,
        "last_completed": last_completed,
    }


def _format_last_completed_phrase(entry: dict[str, Any]) -> str:
    """Render a ``last_completed`` entry as a human-readable phrase."""
    return (
        f"line {entry['line']} [{entry['name']}] " f"({entry['duration_seconds']:.3g}s)"
    )


# ---------------------------------------------------------------------------
# Shared post-compilation result builder
# ---------------------------------------------------------------------------


def _truncate_error_text(text: str, include_warnings: bool) -> str:
    """Normalize a raw stderr blob for a compile-error ``error`` field.

    Drops warning lines when *include_warnings* is False, then tail-truncates
    to :data:`_MAX_ERROR_LENGTH` (keeping the end, where the actual error
    sits).  Dropping before truncating preserves more of the error tail than
    the reverse.  Shared by :func:`_build_compile_result`'s no-position
    fallback and :func:`_dune_dependency_error_result` so the truncation
    policy cannot drift between them.
    """
    text = text.strip()
    if not include_warnings:
        text = _drop_warning_lines(text).strip()
    if len(text) > _MAX_ERROR_LENGTH:
        text = text[-_MAX_ERROR_LENGTH:]
    return text


def _build_compile_result(
    result: dict[str, Any],
    source: str,
    timeout: int,
    include_warnings: bool,
    *,
    file_label: str = _PROOF_FILE_LABEL,
    clean_tmp_paths: bool = True,
    timing_field: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a structured result dict from a coqc subprocess result.

    Shared by ``run_compile`` (inline source) and ``run_compile_file`` (on-disk).

    Parameters
    ----------
    result : dict from ``_run_coqc`` / ``_run_coqc_file``
    source : the Rocq source text (for error context extraction)
    timeout : the timeout value used (for the timeout error message)
    include_warnings : passed to ``_format_error``
    file_label : label used in ``_format_error`` and fallback path cleaning
    clean_tmp_paths : if True, replace tmp file paths in fallback errors
    timing_field : when not None, the pre-built ``timing`` response field
        from :func:`_build_timing_field`.  Attached to every return path
        so callers see partial timings even on timeout or build failure.
        On timeout, the ``last_completed`` entry is also woven into the
        ``error`` string so the agent sees where coqc was stuck.
    """
    if result["timed_out"]:
        error_str = (
            f"Compilation timed out after {timeout}s. "
            "The proof may contain a diverging tactic. "
            "Retry with timing=True to identify the slowest sentence."
        )
        if timing_field is not None and timing_field.get("last_completed"):
            error_str = (
                f"Compilation timed out after {timeout}s. "
                "Last completed sentence: "
                f"{_format_last_completed_phrase(timing_field['last_completed'])}."
            )
        timeout_result: dict[str, Any] = {
            "success": False,
            "reason": "timeout",
            "error": error_str,
        }
        if timing_field is not None:
            timeout_result["timing"] = timing_field
        return timeout_result

    if result["returncode"] == 0:
        # With ``-time`` active, stdout is flooded with ``Chars ... secs``
        # entries; strip them from the displayable ``output`` so the user
        # sees the actual coqc messages without the timing-line firehose.
        stdout_for_output = (
            _strip_timing_lines(result["stdout"])
            if timing_field is not None
            else result["stdout"]
        )
        success_result: dict[str, Any] = {
            "success": True,
            "output": stdout_for_output[:2000],
        }
        if timing_field is not None:
            success_result["timing"] = timing_field
        return success_result

    # Older coqc versions may interleave timing on stderr; strip there too
    # so ``_format_error``'s diagnostic-block regex isn't fed phantom
    # body text.  Cheap when stderr has no timing lines.
    stderr_for_format = (
        _strip_timing_lines(result["stderr"])
        if timing_field is not None
        else result["stderr"]
    )

    error_text = _format_error(
        stderr_for_format,
        source,
        include_warnings=include_warnings,
        file_label=file_label,
    )
    if not error_text:
        raw = stderr_for_format
        if clean_tmp_paths:
            # Scrub tmp paths before truncation so paths in the tail are
            # cleaned too (regex is line-local, so ordering vs. warning-drop
            # doesn't matter for the surviving lines).
            raw = _TMP_PATH_RE.sub(f'"{file_label}"', raw)
        fallback = _truncate_error_text(raw, include_warnings)
        if not fallback:
            fallback = f"coqc exited with code {result['returncode']} (no stderr)."
        fallback_result: dict[str, Any] = {
            "success": False,
            "reason": "compile_error",
            "error": fallback,
        }
        if timing_field is not None:
            fallback_result["timing"] = timing_field
        return fallback_result

    positions = _parse_coqc_error_positions(
        stderr_for_format, include_warnings=include_warnings
    )
    result_dict: dict[str, Any] = {
        "success": False,
        "reason": "compile_error",
        "error": error_text,
    }
    if positions:
        result_dict["error_positions"] = positions
        result_dict["hint"] = (
            "Use rocq_start(file=..., line=..., character=...) to start "
            "an interactive session at the error position, then "
            "rocq_check or rocq_step_multi to explore fixes."
        )
    else:
        result_dict["hint"] = (
            "Use rocq_check for faster iteration, "
            "or rocq_step_multi to explore alternative tactics."
        )
    if timing_field is not None:
        result_dict["timing"] = timing_field
    return result_dict


# ---------------------------------------------------------------------------
# Tool: rocq_compile (core implementation)
# ---------------------------------------------------------------------------


def _find_sentence_end(text: str) -> int | None:
    """Find the first Rocq sentence-terminating dot in *text*.

    A sentence-terminating dot is a ``.`` that is:
    - NOT inside a ``(* ... *)`` comment (arbitrarily nested), and
    - NOT inside a ``"..."`` string literal, and
    - followed by whitespace or end-of-string.

    Returns the index of the dot, or ``None`` if no terminating dot is found.
    """
    for idx, ch, in_comment, in_str in _rocq_scan(text):
        if ch == "." and not in_comment and not in_str:
            if idx + 1 >= len(text) or text[idx + 1] in (" ", "\t", "\n", "\r"):
                return idx
    return None


# Focus / bullet tokens are sentences in their own right but carry no
# terminating dot: a lone ``{`` / ``}`` brace, or a maximal run of one
# bullet character (``-``, ``+``, ``*``).  ``_find_sentence_end`` only
# recognizes dot-terminated sentences, so without special handling these
# tokens are silently dropped by the splitter (e.g. a body of just ``{``
# produces zero commands).
_LEADING_FOCUS_RE = re.compile(r"\{|\}|-+|\++|\*+")


def _leading_focus_token(text: str) -> tuple[str, int] | None:
    """Detect a leading focus/bullet token in *text*.

    Skips leading whitespace, then matches a lone ``{`` / ``}`` brace or
    a maximal run of a single bullet character (``-``/``+``/``*``).
    Returns ``(token, end_index)`` where *end_index* is the offset in the
    original *text* just past the token, or ``None`` when *text* does not
    begin with such a token.

    A ``{`` immediately followed by ``|`` is treated as record syntax
    (``{| ... |}``), not a focus brace, and yields ``None``.

    This detector is position-naive: it assumes *text* is the start of a
    proof-script sentence, where a leading ``-``/``+``/``*`` is a bullet.
    It does *not* distinguish a bullet from a term that happens to begin
    with one of those characters as a binary operator (e.g. a sentence
    starting ``* 2`` or ``- 3``).  That case does not arise for the
    tactic/bullet bodies this splitter is used on; callers feeding
    arbitrary term fragments should not rely on it.
    """
    offset = len(text) - len(text.lstrip())
    rest = text[offset:]
    m = _LEADING_FOCUS_RE.match(rest)
    if not m:
        return None
    tok = m.group(0)
    if tok == "{" and rest[1:2] == "|":
        return None
    return tok, offset + m.end()


def _is_focus_token(text: str) -> bool:
    """True if *text* is exactly one focus/bullet token.

    That is, ignoring surrounding whitespace, *text* is a lone ``{`` /
    ``}`` brace or a single maximal run of one bullet character.  Used to
    decide that a command must NOT have a terminating ``.`` appended:
    Rocq rejects ``-.`` and friends.  ``- reflexivity`` is *not* a focus
    token (it carries a trailing tactic) and does take a dot.
    """
    stripped = text.strip()
    focus = _leading_focus_token(stripped)
    return focus is not None and focus[1] == len(stripped)


def _split_rocq_sentences(source: str) -> list[str]:
    """Split Rocq source into individual sentences.

    Uses :func:`_find_sentence_end` repeatedly to split on
    sentence-terminating dots (handling comments and strings correctly).
    Focus and bullet tokens (``{``, ``}``, and runs of ``-``/``+``/``*``)
    are emitted as standalone sentences even though they carry no
    trailing dot — see :func:`_leading_focus_token`.  These tokens are
    emitted bare (without a dot): Rocq rejects a trailing ``.`` after a
    brace or bullet (e.g. ``-.`` is a syntax error).
    """
    sentences: list[str] = []
    remaining = source
    while remaining.strip():
        focus = _leading_focus_token(remaining)
        if focus is not None:
            token, end = focus
            sentences.append(token)
            remaining = remaining[end:]
            continue
        dot = _find_sentence_end(remaining)
        if dot is None:
            break
        sentence = remaining[: dot + 1].strip()
        if sentence:
            sentences.append(sentence)
        remaining = remaining[dot + 1 :]
    return sentences
