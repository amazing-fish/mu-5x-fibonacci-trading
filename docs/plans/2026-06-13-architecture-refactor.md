# Architecture Refactor Implementation Plan

> **For AI:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Refactor the project into clear data, strategy, research, experiment, selection, execution, and visualization areas while preserving current CLI and report behavior.

**Architecture:** The migration is compatibility-first. Existing top-level modules remain as wrappers while implementation moves into non-conflicting domain packages. Use `market_data`, `strategies`, and `commands` rather than `data`, `strategy`, and `cli` package names because the project already has `data.py`, `strategy.py`, and `cli.py` files.

**Tech Stack:** Python 3, standard library, `unittest`, current CSV/HTML/Markdown report files, existing OKX/Binance public data code.

---

### Task 1: Create Package Skeletons

**Files:**
- Create: `mu_strategy/market_data/__init__.py`
- Create: `mu_strategy/market_data/providers/__init__.py`
- Create: `mu_strategy/core/__init__.py`
- Create: `mu_strategy/strategies/__init__.py`
- Create: `mu_strategy/strategies/presets/__init__.py`
- Create: `mu_strategy/research/__init__.py`
- Create: `mu_strategy/experiments/__init__.py`
- Create: `mu_strategy/selection/__init__.py`
- Create: `mu_strategy/execution/__init__.py`
- Create: `mu_strategy/viz/__init__.py`
- Create: `mu_strategy/commands/__init__.py`

**Step 1: Write the failing import smoke test**

Add `tests/test_architecture_packages.py`:

```python
import importlib
import unittest


class ArchitecturePackageTests(unittest.TestCase):
    def test_domain_packages_are_importable(self):
        for name in [
            "mu_strategy.market_data",
            "mu_strategy.market_data.providers",
            "mu_strategy.core",
            "mu_strategy.strategies",
            "mu_strategy.research",
            "mu_strategy.experiments",
            "mu_strategy.selection",
            "mu_strategy.execution",
            "mu_strategy.viz",
            "mu_strategy.commands",
        ]:
            importlib.import_module(name)
```

**Step 2: Run test to verify it fails**

Run:

```powershell
python -m unittest tests.test_architecture_packages
```

Expected: import errors for missing packages.

**Step 3: Add package skeletons**

Create the listed `__init__.py` files with short package docstrings.

**Step 4: Verify**

Run:

```powershell
python -m unittest tests.test_architecture_packages
python -m unittest discover -s tests
```

Expected: all tests pass.

**Step 5: Commit**

```powershell
git add mu_strategy tests/test_architecture_packages.py
git commit -m "refactor: add architecture package skeletons"
```

### Task 2: Move Data Provider Logic Behind Compatibility Wrapper

**Files:**
- Create: `mu_strategy/market_data/cache.py`
- Create: `mu_strategy/market_data/providers/binance.py`
- Create: `mu_strategy/market_data/providers/okx.py`
- Modify: `mu_strategy/data.py`
- Modify: `tests/test_data.py`

**Step 1: Write failing tests for new imports**

Extend `tests/test_data.py`:

```python
from mu_strategy.market_data.cache import cache_path, merge_incremental_candles
from mu_strategy.market_data.providers.okx import okx_row_to_candle
```

Expected behavior is the same as current tests.

**Step 2: Run targeted test**

```powershell
python -m unittest tests.test_data
```

Expected: import failure.

**Step 3: Move functions**

Move:

- `cache_path`, `read_csv`, `write_csv`, `cached_historical`, `merge_incremental_candles` to `market_data/cache.py`.
- Binance fetch functions to `market_data/providers/binance.py`.
- OKX fetch functions to `market_data/providers/okx.py`.

Keep `mu_strategy/data.py` importing and re-exporting the same names so existing callers keep working.

**Step 4: Verify**

```powershell
python -m unittest tests.test_data
python -m unittest discover -s tests
python -m mu_strategy.cli --days 180 --strategy baseline --report reports\mu_okx_backtest.md
```

Expected: tests pass and report regenerates.

**Step 5: Commit**

```powershell
git add mu_strategy/data.py mu_strategy/market_data tests/test_data.py reports/mu_okx_backtest.md data/OKX_MU-USDT-SWAP_15m_180d.csv data/OKX_MU-USDT-SWAP_1h_180d.csv
git commit -m "refactor: split market data providers"
```

### Task 3: Move Strategy Registry and Components

**Files:**
- Create: `mu_strategy/strategies/components.py`
- Create: `mu_strategy/strategies/registry.py`
- Create: `mu_strategy/strategies/presets/mu.py`
- Modify: `mu_strategy/strategy.py`
- Modify: `tests/test_strategy_rules.py`

**Step 1: Write failing imports**

Add imports in `tests/test_strategy_rules.py`:

```python
from mu_strategy.strategies.registry import default_strategy_groups, selected_strategy_groups
from mu_strategy.strategies.components import StrategyComponents
```

**Step 2: Run test**

```powershell
python -m unittest tests.test_strategy_rules
```

Expected: import failure.

**Step 3: Move definitions**

Move:

- `StrategyComponents` to `strategies/components.py`.
- Strategy group factory functions and selection registry to `strategies/registry.py`.
- MU-specific group list to `strategies/presets/mu.py` if useful.

Keep `mu_strategy/strategy.py` re-exporting old names and hosting executable rule helpers until a later phase.

**Step 4: Verify**

```powershell
python -m unittest tests.test_strategy_rules
python -m unittest tests.test_walk_forward
python -m unittest discover -s tests
```

Expected: all tests pass.

**Step 5: Commit**

```powershell
git add mu_strategy/strategy.py mu_strategy/strategies tests/test_strategy_rules.py
git commit -m "refactor: split strategy group registry"
```

### Task 4: Move Walk-Forward Experiments

**Files:**
- Create: `mu_strategy/experiments/walk_forward.py`
- Modify: `mu_strategy/walk_forward.py`
- Modify: `tests/test_walk_forward.py`

**Step 1: Write failing imports**

Change tests to import core functions from:

```python
from mu_strategy.experiments.walk_forward import render_strategy_group_report
```

**Step 2: Run targeted tests**

```powershell
python -m unittest tests.test_walk_forward
```

Expected: import failure.

**Step 3: Move implementation**

Move walk-forward data classes, split logic, report rendering, and HTML dashboard rendering to `experiments/walk_forward.py`.

Keep `mu_strategy/walk_forward.py` as a CLI wrapper that imports `main` from the new module.

**Step 4: Verify**

```powershell
python -m unittest tests.test_walk_forward
python -m mu_strategy.walk_forward --window-days 180 --windows 1 --report reports\mu_okx_strategy_group_review.md --html-report reports\mu_okx_strategy_components.html
```

Expected: tests pass and reports regenerate.

**Step 5: Commit**

```powershell
git add mu_strategy/walk_forward.py mu_strategy/experiments/walk_forward.py tests/test_walk_forward.py reports/mu_okx_strategy_group_review.md reports/mu_okx_strategy_components.html
git commit -m "refactor: move walk-forward experiments"
```

### Task 5: Move Visualization Rendering

**Files:**
- Create: `mu_strategy/viz/backtest.py`
- Modify: `mu_strategy/visualize.py`
- Modify: `tests/test_visualize.py`

**Step 1: Write failing imports**

Change visualization tests to import:

```python
from mu_strategy.viz.backtest import render_html_visualization
```

**Step 2: Run test**

```powershell
python -m unittest tests.test_visualize
```

Expected: import failure.

**Step 3: Move rendering**

Move `render_html_visualization` and helper functions into `viz/backtest.py`. Keep `visualize.py` as CLI wrapper.

**Step 4: Verify**

```powershell
python -m unittest tests.test_visualize
python -m mu_strategy.visualize --days 180 --strategy baseline --chart-interval 1h --output reports\mu_okx_baseline_backtest.html
```

Expected: tests pass and HTML regenerates.

**Step 5: Commit**

```powershell
git add mu_strategy/visualize.py mu_strategy/viz/backtest.py tests/test_visualize.py reports/mu_okx_baseline_backtest.html
git commit -m "refactor: move backtest visualization"
```

### Task 6: Add Research and Selection Entry Points

**Files:**
- Create: `mu_strategy/research/mu_current.py`
- Create: `mu_strategy/selection/basket.py`
- Create: `tests/test_research_selection.py`

**Step 1: Write failing tests**

Test that:

- `mu_current.current_mu_strategy_name()` returns `baseline`.
- `selection.basket.rank_candidates()` accepts result rows and returns sorted candidates without network access.

**Step 2: Run tests**

```powershell
python -m unittest tests.test_research_selection
```

Expected: import failure.

**Step 3: Implement minimal code**

Add small pure functions only. Do not move all reports yet.

**Step 4: Verify**

```powershell
python -m unittest tests.test_research_selection
python -m unittest discover -s tests
```

Expected: all tests pass.

**Step 5: Commit**

```powershell
git add mu_strategy/research mu_strategy/selection tests/test_research_selection.py
git commit -m "feat: add research and selection entry points"
```

### Task 7: Add Execution Planning Boundary

**Files:**
- Create: `mu_strategy/execution/decision.py`
- Create: `mu_strategy/execution/plan.py`
- Create: `tests/test_execution.py`

**Step 1: Write failing tests**

Test that a fixed strategy can produce:

- `allow`, `wait`, or `block` decision.
- planned margin steps.
- initial stop from current baseline config.

Use fixture candles. Do not fetch network data.

**Step 2: Run tests**

```powershell
python -m unittest tests.test_execution
```

Expected: import failure.

**Step 3: Implement pure planning logic**

Keep this non-trading. No broker API, no order placement.

**Step 4: Verify**

```powershell
python -m unittest tests.test_execution
python -m unittest discover -s tests
```

Expected: all tests pass.

**Step 5: Commit**

```powershell
git add mu_strategy/execution tests/test_execution.py
git commit -m "feat: add execution planning boundary"
```

### Task 8: Update Documentation and Final Validation

**Files:**
- Modify: `README.md`
- Modify: `SKILL.md`
- Optionally create: `docs/architecture.md`

**Step 1: Update docs**

Document:

- New package areas.
- Old command compatibility.
- Recommended commands for data, experiment, selection, and execution planning.

**Step 2: Run final checks**

```powershell
python -m unittest discover -s tests
$env:PYTHONPYCACHEPREFIX = Join-Path $env:TEMP 'mu_strategy_pycache_check'
python -m py_compile mu_strategy\data.py mu_strategy\strategy.py mu_strategy\walk_forward.py mu_strategy\cli.py mu_strategy\visualize.py
python -m mu_strategy.walk_forward --window-days 180 --windows 1 --report reports\mu_okx_strategy_group_review.md --html-report reports\mu_okx_strategy_components.html
python -m mu_strategy.cli --days 180 --strategy baseline --report reports\mu_okx_backtest.md
python -m mu_strategy.visualize --days 180 --strategy baseline --chart-interval 1h --output reports\mu_okx_baseline_backtest.html
```

Expected: all pass, reports regenerate.

**Step 3: Commit**

```powershell
git add README.md SKILL.md docs reports data
git commit -m "docs: document layered research architecture"
```

## Execution Recommendation

Run this plan sequentially. Do not parallelize package moves because import compatibility and report regeneration are shared state.
