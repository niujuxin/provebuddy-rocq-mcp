# Development

Notes for people working on this repository.

## Repository layout

    provebuddy-rocq-mcp/
    |-- core/                    # borrowed engine, organized as ours
    |   |-- __init__.py          # package init (__version__)
    |   |-- config.py            # env-var configuration, availability checks
    |   |-- workspace.py         # workspace/path/dune/.vo-epoch helpers
    |   |-- pet.py               # pet lifecycle, locks, watchdog, _run_with_pet
    |   |-- envelope.py          # failure envelopes, reason sets
    |   |-- state.py             # make_runtime_state() / shutdown_runtime()
    |   |-- compile.py           # run_compile / run_compile_file / run_verify
    |   |-- coqc.py              # coqc subprocess execution + output parsing
    |   |-- interactive.py       # run_query / run_start / run_check / ...
    |   |-- sessions.py          # state table, import cache, staleness
    |   |-- verify.py            # verification source builders
    |   |-- proof_walk.py        # multi-error file walker
    |   |-- compile_enrichment.py# pet state capture on compile errors
    |   |-- diag.py              # diagnostics snapshot builder
    |   `-- health.py            # toolchain health / switch detection
    |-- server/                  # our MCP layer (thin FastMCP 3.x, STDIO)
    |   |-- __init__.py          # FastMCP app, lifespan, registration, main()
    |   |-- __main__.py          # python -m server
    |   `-- tools/               # verbatim upstream @mcp.tool wrappers (10)
    |       |-- __init__.py      # TOOLS registry
    |       |-- compile.py       # rocq_compile / rocq_compile_file / rocq_verify
    |       |-- query.py         # rocq_query / rocq_assumptions / rocq_toc / rocq_notations
    |       `-- interactive.py   # rocq_start / rocq_step_multi / rocq_check
    |-- utils/                   # pure helpers (reserved; empty)
    |-- tests/                   # upstream tests, imports re-pointed to core.*
    |-- specifications/          # migration spec, baseline & triage records
    |-- scripts/                 # infrastructure scripts
    |-- third_party/rocq-mcp/    # git submodule: upstream rocq-mcp (read-only)
    |-- pyproject.toml
    |-- README.md
    |-- DEVELOPMENT.md           # this file
    |-- LICENSE                  # Apache-2.0
    `-- NOTICE

## Getting started

    ./scripts/init.sh   # initializes the submodule and verifies the pin
    uv pip install -e ".[dev]"
    pytest tests/

**Note:** pytest must be run outside any sandbox that interferes with
asyncio (the suite hangs inside the Codex sandbox; run it unsandboxed).

## Tracked upstream version

| Item | Value |
|------|-------|
| Upstream repository | <https://github.com/LLM4Rocq/rocq-mcp> |
| Pinned commit | `6983113d0844c0b7f987c79dab13988445109bfb` |
| Version | `0.3.1` (pyproject.toml) |
| Pinned on | 2026-08-16 |

## Relationship to upstream

`core/` is a self-contained reorganization of upstream `rocq-mcp`'s engine.
No upstream package is imported; no `rocq_mcp` package exists in this
repository.  The FastMCP layer was removed from `core/` and rebuilt as our
own thin MCP server in `server/` (10 of upstream's 13 tools, wrappers
ported verbatim; `rocq_diag` / `rocq_health` / `rocq_switch` deferred).
The permanent references for this relationship are
`specifications/engine-migration-report.md` and
`specifications/mcp-layer-plan.md`.

### Upstream symbol → core module map

Use this table when porting upstream changes:

| Upstream location | Now lives in |
|---|---|
| `server.py` configuration section | `core/config.py` |
| `server.py` shared helpers + .vo epoch + dune | `core/workspace.py` |
| `server.py` pet section + semaphore + watchdog + `_run_with_pet` | `core/pet.py` |
| `server.py` envelope helpers | `core/envelope.py` |
| `server.py` lifespan body | `core/state.py` (`make_runtime_state` / `shutdown_runtime`) |
| `compile.py` §1-717 + sentence splitting | `core/coqc.py` |
| `compile.py` §718-1744 (run_*, dune, verify) | `core/compile.py` |
| `interactive.py` import cache/state table/staleness | `core/sessions.py` |
| `interactive.py` run_* + goal formatting | `core/interactive.py` |
| `verify.py`, `proof_walk.py`, `compile_enrichment.py`, `diag.py`, `health.py` | same name in `core/` |
| `server.py` FastMCP app + lifespan | `server/__init__.py` (lifespan delegates to `core.state`) |
| `server.py` 10 proof `@mcp.tool` wrappers | `server/tools/{compile,query,interactive}.py` (verbatim) |
| deferred: `rocq_diag` / `rocq_health` / `rocq_switch` wrappers | — (not ported; see plan §2) |

MCP-layer conventions:

- Wrappers are plain `async def` functions (no decorator); `server/__init__.py`
  registers them via `mcp.add_tool`.  Context arrives as the legacy FastMCP
  3.x type-hint injection (`ctx: Context = None`), so tests can call wrappers
  directly with a duck-typed mock context.
- Wrapper bodies reference the envelope/`run_*` helpers by their imported
  names, so tests monkeypatch `server.tools.*` (wrapper-module bindings) and
  `core.envelope` / `core.workspace` / `core.config` (helper internals).
- `core/` stays engine-only: no fastmcp imports, no MCP symbols.

Import conventions that must be preserved:

- Engine modules read configuration and shared helpers through module
  references (`_config.X`, `_pet.X`, `_workspace.X`, `_envelope.X`) so test
  monkeypatching of `core.config.X` etc. stays visible at call time.
- `core/sessions.py` registers `_invalidate_import_cache` and
  `_state_invalidate_all` on `pet._pet_invalidation_hooks` at import time.
- `core/workspace.py::_count_sessions_in_workspace` lazily imports
  `core.sessions._state_table` inside the function body (avoids a cycle,
  mirroring upstream).
- `core/compile_enrichment.py` keeps its module-level asserts against
  `envelope._PET_SIDE_FAILURE_REASONS`.

## Syncing with upstream

1. Run `./scripts/sync-upstream.sh` to check for updates (read-only).
2. Review the upstream commits since the pin.
3. Check out the new commit in `third_party/rocq-mcp`, `git add` it, and
   update the pin record in this file.
4. Port engine-layer changes into `core/` using the symbol map above; never
   touch upstream (the submodule is a read-only reference).
5. Re-run `pytest tests/` and compare against the baseline record in
   `specifications/engine-migration-report.md` (§5 Phase 0).

### Port log

| Upstream version | Pinned commit | Ported | Skipped | Notes |
|------------------|---------------|--------|---------|-------|
| 0.3.1 | `6983113d...` | Engine layer (full) | FastMCP layer (all) | Initial migration; see specs |
| 0.3.1 | `6983113d...` | MCP layer (10 proof tools) | `rocq_diag` / `rocq_health` / `rocq_switch` | Thin FastMCP 3.x layer; see `specifications/mcp-layer-plan.md` |

## Roadmap

- Add `rocq_diag` / `rocq_health` / `rocq_switch` and their wrapper tests
  (see `specifications/mcp-layer-plan.md` §2).
- Keep the wrapper bodies in lockstep with upstream on future syncs (they
  are verbatim ports; the symbol map above shows where each lives).

## License

See [LICENSE](LICENSE) and [NOTICE](NOTICE).  The `third_party/rocq-mcp`
submodule is the rocq-mcp project, Copyright 2024 Inria (upstream LICENSE)
and maintained by the LLM4Rocq contributors, licensed under the Apache
License, Version 2.0.  `core/`, `server/tools/`, and the re-pointed tests
are derived works of that project under the same license; see NOTICE for
the attribution statement.
