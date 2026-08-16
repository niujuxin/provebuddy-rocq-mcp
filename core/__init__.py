"""provebuddy-rocq-mcp — Rocq/Coq proof engine.

The ``core`` package is the engine layer, borrowed from upstream
``rocq-mcp`` (see specifications/engine-migration-spec.md) and organized
as our own self-contained code.  No MCP layer lives here; the engine is
exposed as plain functions (``run_*``) plus a runtime-state factory
(``core.state``).
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("provebuddy-rocq-mcp")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
