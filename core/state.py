"""Core runtime state: plain-function replacement for the FastMCP lifespan.

Upstream exposed this as a FastMCP ``lifespan`` context manager
(``app_lifespan``).  Here it is two plain functions so the engine can be
driven without MCP.  The state dict shape and teardown behavior are
unchanged (rocq-mcp @ 6983113d0844c0b7f987c79dab13988445109bfb).
"""

from __future__ import annotations

import collections
import os
from pathlib import Path
from typing import Any

from core import config as _config
from core.pet import _kill_pet
from core.workspace import _cleanup_coqc_artifacts


def make_runtime_state() -> dict[str, Any]:
    """Create the runtime state dict.  Pet is spawned lazily on first use."""
    return {
        "pet_client": None,
        "workspace": _config.ROCQ_WORKSPACE,
        "pet_timeout": _config.ROCQ_PET_TIMEOUT,
        "current_workspace": None,
        # Diagnostics (rocq_diag tool, see _build_diag_snapshot).
        "pet_started_at": None,
        # Count of successful spawns; pet_restarts is derived as
        # max(0, total_spawns - 1).  Counting only successful spawns
        # ensures a fresh server reports 0 restarts even if the very
        # first spawn attempt raised.
        "total_spawns": 0,
        "peak_pet_rss_mb": 0.0,
        "pet_generation": 0,
        "recent_errors": collections.deque(maxlen=_config._RECENT_ERRORS_MAX),
    }


def shutdown_runtime(state: dict[str, Any]) -> None:
    """Tear down the runtime state: kill pet and clean the cache file."""
    client = state.get("pet_client")
    if client:
        _kill_pet(client)
    # Clean up cache file
    ws = state.get("workspace")
    if ws:
        cache_file = Path(ws) / f"rocq_mcp_cache_{os.getpid()}_.v"
        _cleanup_coqc_artifacts(str(cache_file))
