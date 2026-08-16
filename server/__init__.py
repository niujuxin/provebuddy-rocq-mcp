"""FastMCP server: thin STDIO layer over the ``core`` proof engine.

Mirrors upstream ``rocq_mcp.server`` (rocq-mcp @
6983113d0844c0b7f987c79dab13988445109bfb): the same tool set (minus the
deferred diagnostics tools), the same wrapper bodies, the same envelope
contract.  The only structural change is that the lifespan body delegates to
``core.state.make_runtime_state`` / ``shutdown_runtime`` instead of inlining
the state dict.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan

from core.state import make_runtime_state, shutdown_runtime
from server.tools import TOOLS


@lifespan
async def app_lifespan(server: Any) -> Any:
    """Server lifespan. Pet is spawned lazily on first pytanque call."""
    state: dict[str, Any] = make_runtime_state()
    try:
        yield state
    finally:
        shutdown_runtime(state)


mcp = FastMCP("provebuddy-rocq-mcp", lifespan=app_lifespan)

for _tool in TOOLS:
    mcp.add_tool(_tool)


def main() -> None:
    """Run the MCP server over STDIO."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
