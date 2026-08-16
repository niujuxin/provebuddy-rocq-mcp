# provebuddy-rocq-mcp

Rocq/Coq proof engine, reorganized from the
[rocq-mcp](https://github.com/LLM4Rocq/rocq-mcp) project into a
self-contained package.  This repository currently contains the **engine
layer only** (`core/`): the upstream FastMCP server and its 13 tool
wrappers are removed for now.  Our own MCP exposure layer will be built on
top of `core` in a later round.

## What is here

- `core/` — the borrowed engine, organized as ours:
  - `config.py` — env-var configuration and availability checks
  - `workspace.py` — workspace validation, path resolution, dune support,
    `.vo` epoch tracking
  - `pet.py` — pet (pytanque/coq-lsp) subprocess lifecycle, locks,
    memory watchdog, `_run_with_pet`
  - `envelope.py` — failure envelopes and reason sets
  - `state.py` — `make_runtime_state()` / `shutdown_runtime()` (plain
    functions; the upstream FastMCP lifespan, de-MCP'd)
  - `compile.py` + `coqc.py` — coqc-based compile/verify orchestration and
    subprocess/parsing
  - `interactive.py` + `sessions.py` — interactive proof operations and
    session bookkeeping
  - `verify.py`, `proof_walk.py`, `compile_enrichment.py`, `diag.py`,
    `health.py` — moved as-is
- `tests/` — the upstream test suite, re-pointed to `core.*` (it tests
  `core`, not upstream).  MCP-wrapper-specific tests are deferred until the
  MCP layer returns; see
  [specifications/engine-migration-report.md](specifications/engine-migration-report.md).
- `third_party/rocq-mcp/` — pinned upstream submodule, kept as a read-only
  reference for future syncs.

## Requirements

- Python 3.11+
- `coqc` on `PATH` for compile/verify tools (integration tests skip without it)
- `pet` (from [coq-lsp](https://github.com/ejgallego/coq-lsp)) for the
  interactive tools (optional; tests skip without it)

## Install & test

```bash
uv pip install -e ".[dev]"
pytest tests/
```

## Roadmap

- Build our own MCP exposure layer on top of `core` (tool definitions,
  descriptions, parameters, and a possible interactive/batch server split).
- Re-add the deferred wrapper tests once the MCP layer exists.

## License

Apache License, Version 2.0.  See [LICENSE](LICENSE) and [NOTICE](NOTICE).
For contributors, see [DEVELOPMENT.md](DEVELOPMENT.md).
