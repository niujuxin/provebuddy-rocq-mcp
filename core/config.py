"""Core configuration: env-var constants and availability checks.

Extracted verbatim from upstream ``server.py`` section *Configuration*
(rocq-mcp @ 6983113d0844c0b7f987c79dab13988445109bfb).  The only change is
that the fastmcp imports are gone; all constants and functions below are
unchanged.
"""

from __future__ import annotations

import os
import shutil
import warnings

import psutil

# --- Begin verbatim upstream content ---
# ---------------------------------------------------------------------------
# Configuration (env vars with defaults)
# ---------------------------------------------------------------------------

ROCQ_WORKSPACE: str = os.environ.get("ROCQ_WORKSPACE", os.getcwd())
_ROCQ_WORKSPACE_EXPLICIT: bool = "ROCQ_WORKSPACE" in os.environ
ROCQ_COQC_TIMEOUT: int = int(os.environ.get("ROCQ_COQC_TIMEOUT", "60"))
ROCQ_VERIFY_TIMEOUT: int = int(os.environ.get("ROCQ_VERIFY_TIMEOUT", "120"))
ROCQ_PET_TIMEOUT: float = float(os.environ.get("ROCQ_PET_TIMEOUT", "30"))
ROCQ_QUERY_TIMEOUT_CAP: int = int(os.environ.get("ROCQ_QUERY_TIMEOUT_CAP", "300"))
ROCQ_COQC_BINARY: str = os.environ.get("ROCQ_COQC_BINARY", "coqc")
ROCQ_MAX_SOURCE_SIZE: int = int(os.environ.get("ROCQ_MAX_SOURCE_SIZE", "1000000"))


def _check_timeout_config(pet_timeout: float, cap: int) -> str | None:
    """Return a warning if ROCQ_PET_TIMEOUT exceeds ROCQ_QUERY_TIMEOUT_CAP.

    The cap is documented in the README as the upper bound for the
    per-call timeout, but ROCQ_PET_TIMEOUT is the fallback when no
    per-call timeout is given.  If an operator misconfigures the pair
    so the fallback exceeds the cap, the lock can park longer than the
    cap promise — silently violating the documented invariant.
    """
    if pet_timeout > cap:
        return (
            f"ROCQ_PET_TIMEOUT={pet_timeout} exceeds ROCQ_QUERY_TIMEOUT_CAP={cap}; "
            f"calls without a per-call timeout= will park the pet lock longer "
            f"than ROCQ_QUERY_TIMEOUT_CAP claims."
        )
    return None


_timeout_config_msg = _check_timeout_config(ROCQ_PET_TIMEOUT, ROCQ_QUERY_TIMEOUT_CAP)
if _timeout_config_msg:
    warnings.warn(_timeout_config_msg, RuntimeWarning, stacklevel=2)


# Single source of truth for the per-call "pytanque ImportError" envelope hint.
# Used by _ensure_pet, _run_with_pet, run_check, and run_step_multi — all of
# which surface this string in the ``error`` field of their {success:false,
# reason:"unavailable"} envelope.  Centralized so future copy churn cannot
# resurrect a phantom ``pip install 'rocq-mcp[interactive]'`` recipe (no
# ``[interactive]`` extra exists; petanque ships with coq-lsp).
_PYTANQUE_NOT_INSTALLED_HINT = (
    "pytanque is not installed. Petanque (the `pet` binary and the matching "
    "pytanque Python binding) ships with coq-lsp — see "
    "https://github.com/ejgallego/coq-lsp for install instructions "
    "appropriate to your environment."
)


def _check_pet_availability() -> str | None:
    """Return a warning message when pet (pytanque + ``pet`` binary) is missing.

    The interactive tools and the multi-error / state-capture enrichment on
    ``rocq_compile_file`` all route through pet.  Falling back to coqc-only
    operation is a substantial reduction in capability, so the operator
    deserves an up-front signal at server boot rather than discovering it
    only when an agent dispatches the first interactive tool call and gets
    ``reason="unavailable"`` back.

    Returns ``None`` when both halves of the install are present.
    """
    pytanque_missing = False
    try:
        import pytanque  # noqa: F401
    except ImportError:
        pytanque_missing = True
    pet_binary_missing = shutil.which("pet") is None
    if not (pytanque_missing or pet_binary_missing):
        return None
    parts = []
    if pytanque_missing:
        parts.append("the pytanque Python binding is not importable")
    if pet_binary_missing:
        parts.append("the `pet` binary is not on PATH")
    return (
        "pet not detected: " + " and ".join(parts) + ". "
        "Interactive tools (rocq_start / rocq_check / rocq_step_multi / "
        "rocq_query / rocq_assumptions / rocq_toc / rocq_notations) and "
        "proof-state enrichment on rocq_compile_file will return "
        'reason="unavailable". '
        "Petanque (the `pet` binary and the matching pytanque Python "
        "binding) ships with coq-lsp; both halves must be installed "
        "together.  `pip` / `uv` cannot install petanque on their own — "
        "see https://github.com/ejgallego/coq-lsp for install "
        "instructions appropriate to your environment."
    )


_pet_availability_msg = _check_pet_availability()
if _pet_availability_msg:
    warnings.warn(_pet_availability_msg, RuntimeWarning, stacklevel=2)


def _default_max_pet_rss_mb() -> int:
    """Default pet RSS cap: 50% of system RAM, hard-capped at 16 GB.

    Tuned to fire well above legitimate ``vm_compute`` ceilings (~2-4 GB)
    but well below the OOM-killer / swap-thrash zone.  On a 32 GB Mac
    this resolves to 16 GB; on a 16 GB host, 8 GB; on a 64 GB+ host the
    16 GB cap kicks in.
    """
    total_mb = psutil.virtual_memory().total // (1024 * 1024)
    return min(int(0.50 * total_mb), 16_384)


ROCQ_MAX_PET_RSS_MB: int = int(
    os.environ.get("ROCQ_MAX_PET_RSS_MB", str(_default_max_pet_rss_mb()))
)
_MEMORY_WATCHDOG_INTERVAL: float = 0.5
_RECENT_ERRORS_MAX: int = 20

# Multi-error walker tunables for ``rocq_compile_file``.  When CAP is 0
# the feature is disabled and no ``errors`` field is added to the
# response.  TIMEOUT is the per-``pet.run`` budget inside the walker.
_COMPILE_MULTI_ERROR_CAP: int = int(
    os.environ.get("ROCQ_COMPILE_MULTI_ERROR_CAP", "20")
)
_COMPILE_MULTI_ERROR_TIMEOUT: float = float(
    os.environ.get("ROCQ_COMPILE_MULTI_ERROR_TIMEOUT", "5.0")
)

# When True (default) and the compiled file lives in a dune project,
# ``rocq_compile_file`` builds via ``dune build`` so the ``.vo`` lands in
# ``_build/default/…`` instead of shadowing the source tree; the coqc
# fallback (scratch files, ``vos``/``timing`` modes) redirects its output
# there too via ``-o``.  Set ``ROCQ_DUNE_BUILD=0`` to force the legacy
# coqc-into-source-tree behavior everywhere.
