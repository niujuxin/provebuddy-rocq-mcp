# Development

Notes for people working on this repository.

## Repository layout

    provebuddy-rocq-mcp/
    |-- third_party/rocq-mcp/   # git submodule: upstream rocq-mcp (read-only reference / reuse source)
    |-- scripts/                # infrastructure scripts
    |-- README.md               # user-facing documentation
    |-- DEVELOPMENT.md          # this file
    |-- LICENSE                 # Apache-2.0
    |-- NOTICE
    `-- .gitignore

## Getting started

    ./scripts/init.sh   # initializes the submodule and verifies the pin

## Tracked upstream version

| Item | Value |
|------|-------|
| Upstream repository | <https://github.com/LLM4Rocq/rocq-mcp> |
| Pinned commit | `6983113d0844c0b7f987c79dab13988445109bfb` |
| Version | `0.3.1` (pyproject.toml) |
| Pinned on | 2026-08-16 |

## Syncing with upstream

1. Run `./scripts/sync-upstream.sh` to check for updates (read-only).
2. Review the upstream commits since the pin.
3. Check out the new commit in `third_party/rocq-mcp`, `git add` it, and update
   the pin record in this file.
4. Port engine-layer changes selectively and record the port log below.

### Port log

| Upstream version | Pinned commit | Ported | Skipped | Notes |
|------------------|---------------|--------|---------|-------|
| 0.3.1 | `6983113d...` | — | — | Initial pin |

## Roadmap

- Reuse the upstream engine layer (`compile`, `interactive`, `verify`, etc.).
- Build our own MCP exposure layer: tool definitions, descriptions,
  parameters, and a possible split between interactive and batch servers.

## License

See [LICENSE](LICENSE) and [NOTICE](NOTICE). The `third_party/rocq-mcp`
submodule is the rocq-mcp project, Copyright LLM4Rocq contributors, licensed
under the Apache License, Version 2.0.
