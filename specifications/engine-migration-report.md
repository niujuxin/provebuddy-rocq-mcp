# Engine Migration Report

> Status: **Complete** — adopted as the permanent in-repo reference for how this
> repository is organized relative to upstream rocq-mcp, and for how the
> migration was executed and verified.
> Migrated: 2026-08-16
> Upstream baseline: `third_party/rocq-mcp` @ `6983113d0844c0b7f987c79dab13988445109bfb`
> (v0.3.1, confirmed at the time to be the latest commit on upstream `main`)

## 1. Purpose

This repository **borrows** the engine implementation of upstream
[rocq-mcp](https://github.com/LLM4Rocq/rocq-mcp) and reorganizes it as our own
**self-contained** code.  It records:

- Why we borrow upstream code instead of depending on it as a package.
- The target package structure (`core/`, `utils/`, `tests/`).
- The exact migration procedure and what was deleted (the MCP layer) versus
  preserved (the engine logic).
- The verification evidence for each phase.
- The constraints that must not be violated while working in this repository.
- The sync policy for future upstream updates.

## 2. Goals

1. Bring the upstream rocq-mcp engine implementation into this repository and
   reorganize it as our own, **self-contained** code.
2. No `rocq_mcp` package/folder and no dependency on the upstream package; the
   implementation is borrowed from upstream but organized as ours.
3. **No MCP layer in this round**: remove the FastMCP app, the 13 tool
   wrappers, and the entry point.  The engine exists as plain functions
   (`run_*`) with no fastmcp dependency.
4. **Split thousand-line files**: pure code-organization splitting only, no
   implementation-logic changes.
5. Move upstream tests into `tests/`, re-point their imports to `core.*`, and
   treat them as the regression suite for `core`.

## 3. Constraints (Decision Record)

1. Self-contained: all code lives in this repository; we do not import the
   upstream package.
2. No changes to engine implementation logic (the bodies of the `run_*`
   functions stay untouched).  Allowed changes are limited to: file splitting,
   package/import re-pointing, and deleting the MCP wrappers.
3. No MCP layer in this round.
4. Restrained splitting: only split files that exceed ~1,000 lines; do not
   fragment.
5. Tests are the acceptance gate: the same suite must pass before and after the
   migration (pure unit tests green; coqc/pet integration tests skipped
   identically when the binaries are unavailable).
6. No compatibility shims or re-export stubs: deleted symbols must fail loudly
   on import, not silently behave differently.

## 4. Target Structure

```
provebuddy-rocq-mcp/
├── core/                      # Borrowed engine, organized as ours
│   ├── __init__.py            # Package init (includes __version__)
│   ├── config.py              # ← server.py config section: env-var constants
│   ├── workspace.py           # ← server.py shared helpers: workspace/path/dune/vo
│   ├── pet.py                 # ← server.py pet section: subprocess, locks, watchdog, _run_with_pet
│   ├── envelope.py            # ← server.py envelope helpers: _fail, _record_error, etc.
│   ├── state.py               # ← server.py lifespan body, converted to plain functions
│   ├── compile.py             # ← upstream compile.py (run_* orchestration)
│   ├── coqc.py                # ← split: subprocess execution, error/timing parsing, sentence split
│   ├── interactive.py         # ← upstream interactive.py (run_* operations)
│   ├── sessions.py            # ← split: state table, import cache, staleness, tactic path
│   ├── verify.py              # ← upstream verify.py (stays a single file)
│   ├── proof_walk.py          # ← moved as-is
│   ├── compile_enrichment.py  # ← moved as-is
│   ├── diag.py                # ← moved as-is
│   └── health.py              # ← moved as-is
├── utils/                     # Pure helpers (empty package for now)
├── tests/                     # Upstream tests moved in, imports re-pointed to core.*
├── third_party/rocq-mcp/      # Upstream submodule, read-only reference
└── pyproject.toml
```

## 5. Migration Record (per phase)

### Phase 0 — Baseline

**Environment:** Python 3.12.3; `coqc` / `pet` / `dune` **not installed** on
the migration machine, so integration tests are skipped by their upstream
markers.

**Command (run on the unmodified upstream code):**

```bash
cd third_party/rocq-mcp
../.venv/bin/python -m pytest -q -p no:cacheprovider -p faulthandler \
  -o faulthandler_timeout=90 -rA
```

**Result:** `912 passed, 300 skipped, 1 warning in 11.10s` (1212 collected,
0 failed).

| Skip reason | Count |
|---:|---:|
| `coqc not available` | 169 |
| `pet not available` | 99 |
| multi-error tests require coqc + pet on PATH | 20 |
| coqc and pet required for compile error state capture | 7 |
| coqc and pet required for Phase 2 verification | 5 |

**Environment caveat:** the suite hangs inside the Codex sandbox (asyncio
misbehaves there — the event loop never closes after
`test_diag.py::TestPeakRss::test_peak_rss_updated_by_watchdog`).  The exact same
suite completes in ~11s unsandboxed.  All test runs for this migration were
executed **outside** the sandbox.

### Phase 1 — Split server.py into core/ (MCP deleted)

The 3,051-line upstream `server.py` was cut at its section boundaries.  The
~1,700-line kernel was extracted verbatim into five modules; everything MCP
(fastmcp imports, `mcp = FastMCP(...)`, the 13 `@mcp.tool` wrappers, the
re-export import block, and `main()`) was left out.

| New file | Upstream section | Body lines (byte-identical) |
|---|---|---:|
| `config.py` | Configuration | 129 |
| `workspace.py` | Shared helpers + ".vo epoch" + dune | 763 |
| `pet.py` | Pet management + semaphore + watchdog + `_run_with_pet` | 549 |
| `envelope.py` | Envelope helpers (`_record_error`, `_fail`, `_resolve_tool_envelope`, …) | 235 |
| `state.py` | Lifespan body, converted to `make_runtime_state()` / `shutdown_runtime()` | — |

**Verification:** every extracted block `diff`s byte-identical to the upstream
line range (only the module header differs); `state.py`'s dict body and
teardown logic are identical to `app_lifespan` (structure only, no MCP
decorator); `import core` succeeds with no fastmcp.

### Phase 2 — Move engine files & re-point imports

Seven upstream engine files moved into `core/`.  Two were split conservatively
along natural boundaries; the rest moved as-is.

| Upstream file | Result in core/ |
|---|---|
| `compile.py` (1,843 lines) | `compile.py` (run_* orchestration, dune, verify) + `coqc.py` (subprocess, error/timing parsing, sentence split) |
| `interactive.py` (2,152 lines) | `interactive.py` (run_* operations, goal formatting) + `sessions.py` (state table, import cache, staleness, tactic path) |
| `verify.py` / `proof_walk.py` | moved **byte-identical** (`cmp` clean) |
| `compile_enrichment.py` / `diag.py` / `health.py` | moved with import re-pointing only |

**Import re-pointing:** the ~106 `_server.XXX` references (27 distinct
symbols) were mapped to `core.config` / `core.workspace` / `core.pet` /
`core.envelope` by purpose, using **aliased module references**
(`_config.X`, `_pet.X`, `_workspace.X`, `_envelope.X`).  This matters twice:

- It avoids shadowing by parameters/local variables named `workspace` / `pet`
  (a naive `_server.X` → `workspace.X` rename would call attributes on the
  `workspace` parameter).
- It preserves upstream monkeypatch semantics: tests patch
  `core.config.ROCQ_…`, `core.pet._ensure_pet`, etc. and the module-reference
  style keeps those patches visible at call time.

**Import-time behaviors preserved:** `sessions.py` registers
`_invalidate_import_cache` / `_state_invalidate_all` on
`pet._pet_invalidation_hooks` at import; `compile_enrichment.py` keeps its
module-level asserts against `envelope._PET_SIDE_FAILURE_REASONS`;
`workspace.py::_count_sessions_in_workspace` keeps its function-body lazy
import of the state table (re-pointed to `core.sessions._state_table`).

**Verification:** an AST-based pass confirmed every function body in `core/`
matches upstream **modulo the import re-pointing only**; `import core` contains
no fastmcp and no `rocq_mcp` references.

### Phase 3 — Packaging (self-contained)

`pyproject.toml`: project `provebuddy-rocq-mcp` v0.1.0; packages `core`,
`utils`; dependencies **psutil + pytanque (git @v0.2.2) only — no fastmcp**;
no console script (no MCP entry point yet); pytest config (`asyncio_mode =
"auto"`, `testpaths = ["tests"]`).

**Verification:** `uv pip install -e ".[dev]"` succeeds; `import core` works
from any directory with `__version__ == "0.1.0"`.  `fastmcp` and the upstream
`rocq-mcp` package were then **uninstalled** from the venv and `import core`
still works — the environment does not require either.

### Phase 4 — Test migration & triage

The upstream suite (22.8k lines, 25 files) was copied to `tests/` and
re-pointed mechanically:

| Upstream module | Target core modules |
|---|---|
| `rocq_mcp.server` (kernel) | `core.config` / `core.workspace` / `core.pet` / `core.envelope` / `core.state` (per symbol) |
| `rocq_mcp.server` (re-exports) | `core.diag` / `core.compile_enrichment` |
| `rocq_mcp.compile` | `core.compile` / `core.coqc` |
| `rocq_mcp.interactive` | `core.interactive` / `core.sessions` |
| `rocq_mcp.verify` / `diag` / `health` / `proof_walk` / `compile_enrichment` | same name in `core/` |

String-path patches were re-pointed too
(`mock.patch("core.config.ROCQ_WORKSPACE", …)`,
`mock.patch("core.workspace.subprocess.run", …)`), and the conftest
circular-import workaround (`import rocq_mcp.server`) was removed while the
coqc/pet availability flags and skip markers were kept.

**Triage — dropped wrapper tests (74 cases, 23 blocks):** tests that exercised
the deleted `@mcp.tool` wrappers were removed this round (re-added when our own
MCP layer is rebuilt): no-context envelopes, `clamped_timeout` /
`workspace_warning` envelope finalization, per-call timeout clamping,
wrapper-driven `vo_rebuild_warning`, and the `rocq_health` / `rocq_switch` /
`rocq_diag` wrapper paths.

| File | Removed blocks |
|---|---|
| `test_assumptions.py` | `TestRocqAssumptionsWrapper` (4) |
| `test_diag.py` | `TestDiagSchema::test_diag_tool_routes_to_snapshot`, `test_diag_tool_no_context` (2) |
| `test_envelope_contract.py` | `TestWrapperNoContextEnvelope` (8) |
| `test_health.py` | 9 module-level `rocq_health` / `rocq_switch` tests |
| `test_notations.py` | `TestRocqNotationsTimeout::test_above_cap_clamped_with_signal` (1) |
| `test_query.py` | `TestRocqQueryWrapper`, `TestRocqQueryTimeout`, `TestRocqQueryFromStateWrapper` (11) |
| `test_step_multi.py` | `TestStepMultiTimeoutForwarding` (2) |
| `test_toc.py` | `TestRocqTocTimeout::test_above_cap_clamped_with_signal` (1) |
| `test_server.py` | `TestWrapperWorkspaceAutoDetect`, `TestWorkspaceWarning`, `TestVoRebuildWarning`, `TestReadmeUsagePatterns` (36) |

**Result (unsandboxed):**

| Metric | Baseline (upstream) | After migration |
|---|---|---|
| Total collected | 1212 | 1138 |
| Passed | 912 | **838** |
| Skipped | 300 | **300** (identical profile) |
| Failed / errors | 0 | **0** |

The 74-test difference is exactly the dropped wrapper coverage.  The
de-flake-sensitive tests (`test_diag`, `test_timeout`,
`test_memory_watchdog`) were preserved verbatim and pass, so lock-contention
timeout margins, `_PetLockTimeout` ordering, and the 0.5s memory-watchdog
interval semantics are unchanged.

### Phase 5 — Documentation & attribution

- `DEVELOPMENT.md` — repository layout, upstream symbol → core module map,
  sync policy, port log.
- `NOTICE` — Apache-2.0 attribution for the borrowed code (upstream
  LLM4Rocq/rocq-mcp, pin `6983113…`).
- `README.md` — rewritten to describe the engine-only state and roadmap.
- This report consolidates the migration spec and the phase records.

## 6. Intended Deviations from Upstream

Everything in `core/` matches upstream except the following **deliberate**
differences:

1. **Deleted MCP layer** — FastMCP app, 13 `@mcp.tool` wrappers, re-export
   block, `main()` (and `app_lifespan` / `mcp`).  Stale references fail loudly.
2. **Module headers** — new docstrings and per-module imports.
3. **Import re-pointing** — `_server.X` → `_config.X` / `_workspace.X` /
   `_pet.X` / `_envelope.X`; `rocq_mcp.*` → `core.*`; lazy imports updated
   (`core.sessions._state_table`, `core.verify` symbols, …).
4. **`state.py` conversion** — the FastMCP lifespan becomes two plain
   functions with identical state dict and teardown logic.

## 7. Import Conventions to Preserve

- Engine modules read configuration and shared helpers through module
  references (`_config.X`, `_pet.X`, `_workspace.X`, `_envelope.X`) so test
  monkeypatching of `core.config.X` etc. stays visible at call time.
- `core/sessions.py` registers `_invalidate_import_cache` and
  `_state_invalidate_all` on `pet._pet_invalidation_hooks` at import time.
- `core/workspace.py::_count_sessions_in_workspace` lazily imports
  `core.sessions._state_table` inside the function body (avoids a cycle,
  mirroring upstream).
- `core/compile_enrichment.py` keeps its module-level asserts against
  `envelope._PET_SIDE_FAILURE_REASONS`.

## 8. Sync Policy

1. Run `./scripts/sync-upstream.sh` to check for updates (read-only).
2. Review upstream commits since the pin; check out the new commit in
   `third_party/rocq-mcp` and update the pin record in `DEVELOPMENT.md`.
3. Port engine-layer changes into `core/` using the symbol map in
   `DEVELOPMENT.md`; never modify the upstream submodule.
4. Re-run `pytest tests/` and compare against the numbers in §5/§10.

### Port log

| Upstream version | Pinned commit | Ported | Skipped | Notes |
|------------------|---------------|--------|---------|-------|
| 0.3.1 | `6983113d...` | Engine layer (full) | FastMCP layer (all) | Initial migration |

## 9. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Import re-pointing errors (106 `_server.XXX` references mis-targeted) | Baseline + full-suite comparison; per-symbol mapping by purpose; aliased module refs avoid parameter shadowing |
| Split boundary mistakes altering function bodies | Per-block `diff` / AST verification against upstream; only import-line differences allowed |
| pet concurrency/timeout semantics broken by the move | Verbatim moves; watch `test_diag` / `test_timeout` / `test_memory_watchdog` |
| Import-time ordering changes (hook registration, module-level asserts) | Explicitly preserved (see §7); covered by tests |
| Test triage losing coverage | Every removed test recorded (§5 Phase 4) and re-added with the MCP layer |
| Monkeypatch parity lost during re-pointing | All config/helpers read via module references; string patches re-pointed to `core.*` paths |
| Ongoing upstream sync cost | Re-triage against `third_party/rocq-mcp`; symbol map in DEVELOPMENT.md |

## 10. Acceptance Criteria & Verification Results

| Criterion | Result |
|---|---|
| `pytest tests/` green, pure unit tests not skipped | ✅ 838 passed / 300 skipped / 0 failed (unsandboxed) |
| `core/` contains no fastmcp and no `rocq_mcp` references | ✅ grep-clean (only documentation mentions) |
| Engine behavior identical to upstream | ✅ AST-verified function bodies; byte-identical `verify.py` / `proof_walk.py`; full-suite pass |
| Repository self-contained | ✅ `import core` works with fastmcp and rocq-mcp uninstalled |
| Every phase has a deliverable and verification step | ✅ documented in §5 |

## 11. Reproducing the Check

```bash
./scripts/init.sh                    # pin the submodule
uv pip install -e ".[dev]"           # psutil + pytanque, no fastmcp
pytest tests/                        # must be run unsandboxed
python -c "import core"              # no rocq-mcp / fastmcp required
```
