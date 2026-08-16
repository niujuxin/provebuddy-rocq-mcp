# provebuddy-rocq-mcp

Rocq/Coq proof engine with a thin MCP (Model Context Protocol) server,
reorganized from the
[rocq-mcp](https://github.com/LLM4Rocq/rocq-mcp) project into a
self-contained repository.  The engine lives in `core/` (borrowed,
re-organized as ours, engine-only); the MCP exposure layer lives in
`server/` and is a thin FastMCP 3.x STDIO server over the engine — the 10
proof tools from upstream, verbatim (tool names, descriptions, and
parameters unchanged).  The three diagnostics tools (`rocq_diag`,
`rocq_health`, `rocq_switch`) are deferred to a later round.

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
- `server/` — the MCP layer: FastMCP app + lifespan + the 10 tool
  wrappers (`tools/compile.py`, `tools/query.py`, `tools/interactive.py`).
  STDIO only; runs via `python -m server` or the `provebuddy-rocq-mcp`
  console script.
- `tests/` — the upstream test suite, re-pointed to `core.*` and
  `server.tools.*` (it tests our code, not upstream).  See
  [specifications/engine-migration-report.md](specifications/engine-migration-report.md)
  and [specifications/mcp-layer-plan.md](specifications/mcp-layer-plan.md).
- `third_party/rocq-mcp/` — pinned upstream submodule, kept as a read-only
  reference for future syncs.

## Requirements

- Python 3.11+
- `coqc` on `PATH` for compile/verify tools (integration tests skip without it)
- `pet` (from [coq-lsp](https://github.com/ejgallego/coq-lsp)) for the
  interactive tools (optional; tests skip without it)
- The MCP server must be launched in an environment whose `PATH` / opam
  switch resolve the toolchain you intend to use — coqc and pet inherit
  the environment of whatever process starts the server.

## Install & test

```bash
uv pip install -e ".[dev]"
pytest tests/
```

## Running the MCP server

```bash
python -m server
# or
provebuddy-rocq-mcp
```

The server speaks MCP over **STDIO**.  Point an MCP client at it, e.g.:

```json
{
  "mcpServers": {
    "provebuddy": {
      "command": "provebuddy-rocq-mcp",
      "args": []
    }
  }
}
```

The server exposes 10 tools:

- Compile / verify: `rocq_compile`, `rocq_compile_file`, `rocq_verify`
- Query / explore: `rocq_query`, `rocq_assumptions`, `rocq_toc`,
  `rocq_notations`
- Interactive sessions: `rocq_start`, `rocq_check`, `rocq_step_multi`

Each tool's description and parameters match upstream; every response is
the unified envelope `{success, error, reason, ...}`.

## Recommended usage patterns

### Multi-tactic exploration: `rocq_check` then `rocq_step_multi`

To explore N alternative tactics from a known good state, advance the
state with `rocq_check` first, then branch with `rocq_step_multi`:

    # Step 1: confirm the prefix and advance.
    result = rocq_check(from_state=S, body="intros n m H.")
    new_state = result["state_id"]

    # Step 2: try alternatives from that state.
    rocq_step_multi(from_state=new_state, tactics=[
        "by ring.",
        "by lia.",
        "by reflexivity.",
    ])

This is more efficient than passing the prefix repeatedly inside
`tactics=[...]` (each tactic would re-run the prefix).  It also makes the
agent's intent — "I'm confident in the prefix; explore the next step" —
explicit.

### Imports and scopes in `rocq_query`

Statements like `Require Import`, `From X Require Y`, `Open Scope`,
`Set`, `Unset`, `Local`, and `Section` must go in the `preamble=`
parameter (a multi-line string), not in `command=`:

    rocq_query(
        preamble="From Coq Require Import Reals.\nOpen Scope R_scope.",
        command="Search (_ + _).",
    )

Why: each statement in `command=` runs in isolation, so `Open Scope`
in `command=` would not propagate to the next statement.  For
multi-import preambles, prefer `file=<path>` to a `.v` file containing
the imports — more reliable when the imports include `Set` / `Unset`
directives that may need a specific ordering.

For mid-proof queries — e.g. `Search` against the live proof state —
use `from_state=<state_id>` instead of preamble; the live state already
has all imports and scopes set up.

## License

Apache License, Version 2.0.  See [LICENSE](LICENSE) and [NOTICE](NOTICE).
For contributors, see [DEVELOPMENT.md](DEVELOPMENT.md).
