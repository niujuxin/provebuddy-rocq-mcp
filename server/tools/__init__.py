"""MCP tool registry: the 10 tools exposed by the provebuddy server.

Wrappers are plain ``async def`` functions (no ``@mcp.tool`` decorator) so
they stay directly callable in tests with a mock context, matching the
upstream test style.  ``server/__init__.py`` registers them via
``mcp.add_tool``.
"""

from __future__ import annotations

from server.tools.compile import rocq_compile, rocq_compile_file, rocq_verify
from server.tools.interactive import rocq_check, rocq_start, rocq_step_multi
from server.tools.query import rocq_assumptions, rocq_notations, rocq_query, rocq_toc

TOOLS: tuple = (
    rocq_compile,
    rocq_compile_file,
    rocq_verify,
    rocq_query,
    rocq_assumptions,
    rocq_toc,
    rocq_notations,
    rocq_start,
    rocq_step_multi,
    rocq_check,
)

__all__ = [
    "TOOLS",
    "rocq_compile",
    "rocq_compile_file",
    "rocq_verify",
    "rocq_query",
    "rocq_assumptions",
    "rocq_toc",
    "rocq_notations",
    "rocq_start",
    "rocq_step_multi",
    "rocq_check",
]
