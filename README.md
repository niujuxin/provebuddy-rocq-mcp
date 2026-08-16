# provebuddy-rocq-mcp

An [MCP](https://modelcontextprotocol.io/) server that lets AI agents write,
check, and iterate on [Rocq](https://rocq-prover.org/) (formerly Coq) proofs.

Built on top of the [rocq-mcp](https://github.com/LLM4Rocq/rocq-mcp) project,
with a focus on making proof development fast and reliable for agentic
workflows.

## Features

- **Batch compilation** — compile and verify whole `.v` files with the Rocq
  compiler for final, authoritative checks, including audits of admits,
  axioms, and statement mismatches.
- **Interactive proof sessions** — keep imports warm in a background
  interactive backend, inspect the current goal, step through tactics one at
  a time, and try several candidate tactics in a single call.
- **Environment tooling** — search for lemmas, inspect axioms and notations,
  and browse the structure of `.v` files.
- **Operator diagnostics** — health checks and runtime diagnostics for the
  underlying toolchain and interactive backend.

## Requirements

- Python 3.11+
- [Rocq / Coq](https://rocq-prover.org/) — `coqc` must be on your `PATH`
- `pet` (from [coq-lsp](https://github.com/ejgallego/coq-lsp)) — optional but
  recommended; required for the interactive tools
- An MCP client, such as Claude Code or any MCP-capable agent harness

## Installation

Once the package is published:

    uv pip install provebuddy-rocq-mcp

Register the server with your MCP client:

    "mcpServers": {
      "provebuddy-rocq-mcp": {
        "command": "rocq-mcp",
        "type": "stdio"
      }
    }

A resident shared instance over HTTP/SSE is also supported for deployments
where many agent sessions share one server.

## Quick start

1. Call the health-check tool first to confirm which Rocq toolchain the server
   is using.
2. For final verification of a finished proof, compile the whole `.v` file and
   run the staged verification tool to audit admits, axioms, and statement
   mismatches.
3. For fast iteration on a proof, open an interactive session, inspect the
   current goal, and step through tactics until the proof closes.

## Configuration

The server is configured through environment variables (workspace root,
timeouts, state-table size, memory limits, and more). See the documentation
under `docs/` for the full reference.

## Status

Under active development — no release yet. This repository currently contains
the project base; the server implementation is being built.

## License

Apache License, Version 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

For contributors, see [DEVELOPMENT.md](DEVELOPMENT.md).
