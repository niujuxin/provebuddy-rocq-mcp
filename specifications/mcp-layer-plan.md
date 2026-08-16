# MCP Layer Implementation Plan

**Status:** Approved (2026-08-16)

**Repository:** `provebuddy-rocq-mcp` — self-contained Rocq/Coq proof engine
(engine migrated from upstream rocq-mcp into `core/`; see
`engine-migration-report.md`).

## 1. Goal

Add a thin MCP (Model Context Protocol) layer on top of the existing
`core/` engine. The MCP layer is a faithful, thin port of the upstream
`@mcp.tool` wrappers: **no engine changes**, tool names / signatures /
descriptions / parameter descriptions carried over verbatim from upstream.

## 2. Scope

- Expose **10 tools** (upstream's 13 minus the three ops/diagnostics tools,
  which are deferred):

  | Group | Tools |
  |---|---|
  | Compile / verify | `rocq_compile`, `rocq_compile_file`, `rocq_verify` |
  | Query / explore | `rocq_query`, `rocq_assumptions`, `rocq_toc`, `rocq_notations` |
  | Interactive | `rocq_start`, `rocq_step_multi`, `rocq_check` |

- Deferred (not in v1): `rocq_diag`, `rocq_health`, `rocq_switch`.
- Transport: **STDIO only** (`mcp.run(transport="stdio")`). No HTTP/SSE, no
  network features.
- Framework: **FastMCP 3.x** (`fastmcp>=3.0,<4`; current 3.4.7).
- **`core/` must remain untouched** (`git diff core/` empty at acceptance).

## 3. Directory layout

```
server/
  __init__.py        # FastMCP instance, lifespan, tool registration, main()
  __main__.py        # python -m server entry point
  tools/
    __init__.py      # TOOLS registry (imported by server/__init__.py)
    compile.py       # rocq_compile, rocq_compile_file, rocq_verify
    query.py         # rocq_query, rocq_assumptions, rocq_toc, rocq_notations
    interactive.py   # rocq_start, rocq_step_multi, rocq_check
```

Notes:

- The package is named `server/` — **not** `mcp/`, which would shadow the
  MCP Python SDK's `mcp` package that FastMCP 3.x depends on.
- Tool functions are plain `async def` functions (no `@mcp.tool`
  decorator); `server/__init__.py` registers them with `mcp.add_tool()`.
  This keeps wrappers directly callable in tests with a mock context,
  matching upstream's test style.

## 4. Implementation details

### 4.1 Wrapper bodies (verbatim port)

Each wrapper body follows the upstream structure exactly:

- 7 tools (`rocq_compile`, `rocq_compile_file`, `rocq_verify`,
  `rocq_query`, `rocq_assumptions`, `rocq_toc`, `rocq_notations`,
  `rocq_start`) use the shared envelope helpers from `core/envelope.py`:
  `_resolve_tool_envelope(...)` → `run_*(...)` →
  `_finalize_tool_envelope(...)`.
- `rocq_check` / `rocq_step_multi` keep upstream's inline logic
  (`_no_ctx_fail`, `_resolve_call_timeout`, inline `clamped_timeout`
  injection) — they do not use `_resolve_tool_envelope` upstream.
- `rocq_verify` keeps the failure-side `_record_error` call including the
  double-record guard.
- Docstrings are copied **verbatim** from upstream `server.py`. FastMCP
  parses the Google-style `Args:` sections, producing identical tool and
  parameter descriptions in the MCP schema.

### 4.2 Context handling (adaptation is nearly zero)

Upstream at the pinned commit already targets **FastMCP 3.x**
(`fastmcp>=3.1.0`, `from fastmcp import FastMCP, Context`,
`@lifespan` from `fastmcp.server.lifespan`, `ctx: Context = None`,
`mcp.run(transport="stdio")`). The wrappers therefore port **verbatim**,
including `ctx: Context = None` (FastMCP 3.x type-hint injection; `ctx` is
excluded from the tool schema and tests pass a duck-typed `_MockContext`).

The only structural change is the lifespan body: upstream inlined the state
dict + teardown; we delegate to `core.state.make_runtime_state()` /
`shutdown_runtime()` (identical state shape, no behavior change).

### 4.3 Imports (mapping)

| Upstream symbol | Our source |
|---|---|
| `run_compile_with_state`, `run_compile_file_with_state` | `core.compile_enrichment` |
| `run_verify` | `core.compile` |
| `run_query`, `run_assumptions`, `run_toc`, `run_notations`, `run_start`, `run_check`, `run_step_multi` | `core.interactive` |
| `_resolve_tool_envelope`, `_finalize_tool_envelope`, `_no_ctx_fail`, `_record_error` | `core.envelope` |
| `_resolve_call_timeout` | `core.workspace` |
| `ROCQ_COQC_TIMEOUT`, `ROCQ_VERIFY_TIMEOUT`, `ROCQ_QUERY_TIMEOUT_CAP` | `core.config` |

## 5. Dependency & entry point changes

- `pyproject.toml`: add `fastmcp>=3.1,<4` to `dependencies` (upstream floor).
- Add `server*` to `[tool.setuptools.packages.find] include`.
- Add console script: `provebuddy-rocq-mcp = "server:main"`.
- `server/__main__.py` → `from server import main; main()`.

## 6. Test plan

1. **Audit** all `rocq_*` wrapper-name references in `tests/` and classify:
   docstrings/strings (leave), core-behavior tests (leave), real wrapper
   call sites (re-point to `server.tools.*`). Known latent references:
   `tests/test_compile_file.py::TestRocqCompileFileWrapper` and
   `tests/test_multi_error.py` (both currently hidden by coqc/pet skip
   markers).
2. **Restore** the upstream wrapper tests removed during engine migration,
   excluding the diag/health/switch cases (62 tests, 23 blocks):
   `test_assumptions` (4), `test_envelope_contract` (7 of 8 — the 8th is
   the deferred `rocq_diag` no-ctx test), `test_notations` (1),
   `test_query` (12), `test_step_multi` (4), `test_toc` (1),
   `test_server` (33). Imports re-pointed from `rocq_mcp.server` to our
   `server.tools.*`; monkeypatch targets re-pointed to the wrapper-module
   bindings / `core.envelope` / `core.workspace` / `core.config`.
3. **Add smoke tests**: server exposes exactly 10 tools; tool schema
   descriptions match upstream; a no-coqc call to `rocq_compile` returns the
   `unavailable` envelope instead of raising; end-to-end STDIO round trip via
   the FastMCP client.
4. **Run the full suite outside the sandbox** (escalated) — asyncio hangs
   inside the sandbox. Skip profile must stay at 300 with no new skips.

## 7. Acceptance criteria

1. `pytest tests/` (escalated, outside sandbox): all green; skip profile =
   baseline 300.
2. `git diff core/` is empty.
3. AST comparison: wrapper bodies match upstream modulo context/import
   adaptation; docstrings byte-identical.
4. `python -m server` starts; `tools/list` returns the 10 tools; STDIO smoke
   test passes.
5. Docs updated: `README.md` (MCP usage, STDIO config example,
   prerequisites), `DEVELOPMENT.md` (symbol map gains `server.*`).

## 8. Risks

- Upstream is already FastMCP 3.x, so the only adaptation points are import
  re-pointing and delegating the lifespan body to `core.state` (§4).
- Direct-call wrapper tests rely on legacy type-hint context injection,
  which FastMCP 3.x documents as supported. If a future FastMCP removes it,
  tests must move to a client-driven harness (not in v1 scope).
- Package name collision with the MCP SDK avoided by using `server/`.
