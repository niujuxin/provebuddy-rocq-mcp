"""Smoke tests for the MCP layer (`server/`).

Verifies the FastMCP app surface: exactly the 10 tools, schemas generated
from the verbatim upstream docstrings (ctx excluded), graceful failure
envelopes instead of exceptions, and a real STDIO round trip.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from fastmcp import Client

import server
from server.tools import TOOLS


EXPECTED_TOOLS = (
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
)

UPSTREAM_SERVER = (
    Path(__file__).resolve().parent.parent
    / "third_party"
    / "rocq-mcp"
    / "src"
    / "rocq_mcp"
    / "server.py"
)


def _upstream_docstring_first_line(name: str) -> str:
    """First line of the upstream tool's docstring (the tool description)."""
    src = UPSTREAM_SERVER.read_text()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            doc = ast.get_docstring(node)
            assert doc is not None, f"upstream {name} has no docstring"
            return doc.strip().splitlines()[0]
    raise AssertionError(f"upstream tool {name} not found")


@pytest.mark.asyncio
async def test_server_exposes_exactly_ten_tools():
    tools = await server.mcp.list_tools()
    names = [t.name for t in tools]
    assert names == list(EXPECTED_TOOLS)


@pytest.mark.asyncio
async def test_tool_descriptions_match_upstream_docstrings():
    tools = {t.name: t for t in await server.mcp.list_tools()}
    for name in EXPECTED_TOOLS:
        tool = tools[name]
        expected_head = _upstream_docstring_first_line(name)
        assert tool.description is not None
        assert tool.description.startswith(expected_head), (
            f"{name} description diverged from upstream docstring"
        )


@pytest.mark.asyncio
async def test_schema_excludes_ctx_and_keeps_params():
    tools = {t.name: t for t in await server.mcp.list_tools()}
    schema = tools["rocq_compile"].parameters
    props = schema.get("properties", {})
    assert "ctx" not in props
    assert set(props) == {"source", "workspace", "timeout", "include_warnings"}
    assert schema.get("required") == ["source"]
    # Per-parameter descriptions come from the verbatim docstring Args block.
    assert props["source"]["description"].startswith(
        "Complete Rocq (.v) file content to compile."
    )
    assert "timeout" in props


@pytest.mark.asyncio
async def test_compile_returns_envelope_not_exception():
    """A no-coqc call returns the failure envelope instead of raising."""
    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "rocq_compile",
            {"source": "Theorem t : True. Proof. exact I. Qed."},
        )
    data = result.data
    assert isinstance(data, dict)
    assert data.get("success") is False
    assert isinstance(data.get("error"), str) and data["error"]
    assert isinstance(data.get("reason"), str) and data["reason"]


@pytest.mark.asyncio
async def test_stdio_roundtrip():
    """The server runs over STDIO and answers a real tool call."""
    config = {
        "mcpServers": {
            "provebuddy": {
                "command": sys.executable,
                "args": ["-m", "server"],
            }
        }
    }
    async with Client(config) as client:
        tools = await client.list_tools()
        assert [t.name for t in tools] == list(EXPECTED_TOOLS)
        result = await client.call_tool(
            "rocq_compile",
            {"source": "Theorem t : True. Proof. exact I. Qed."},
        )
    data = result.data
    assert isinstance(data, dict)
    assert data.get("success") is False
    assert data.get("reason") in {"compile_error", "unavailable"}


def test_tools_registry_order():
    assert [t.__name__ for t in TOOLS] == list(EXPECTED_TOOLS)
