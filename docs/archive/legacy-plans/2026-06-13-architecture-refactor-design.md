# MU Strategy Research Architecture Refactor Design

> Deprecated legacy plan: this document is archived for historical context only. It predates the trusted-data generation contract and may reference deleted tracked reports or flat CSV caches. Do not use it as a current implementation checklist.

## Background

The project has grown from a single MU backtest workflow into a broader research platform:

- OKX `MU-USDT-SWAP` is now the default data source for MU research.
- Binance `MUUSDT` remains a comparison source.
- Strategy groups now combine entry, position sizing, exit, and filter components.
- Reports include Markdown summaries, HTML dashboards, single-strategy visualizations, cross-asset scans, and experimental ablations.

The current module layout still keeps most responsibilities under flat files such as `data.py`, `strategy.py`, `backtest.py`, `walk_forward.py`, and `visualize.py`. That was reasonable for the initial stage, but the next stage needs clearer boundaries between research, experiments, candidate selection, execution planning, visualization, and data acquisition.

## Goals

1. Separate research workflows from execution workflows.
2. Make strategy groups composable and inspectable by component.
3. Keep MU as the first-class research target while allowing future symbols and venues.
4. Preserve current CLI behavior during migration.
5. Keep backtests reproducible with explicit data source, symbol, date range, and strategy group.
6. Leave room for future paper/live trading without letting automation concerns pollute research code.

## Non-Goals

- Do not implement live trading in this refactor.
- Do not introduce a new framework or database.
- Do not replace the current `unittest` suite.
- Do not move historical reports unless a compatibility wrapper points to the new location.
- Do not hide strategy assumptions behind opaque configuration too early.

## Recommended Package Layout

```text
mu_strategy/
  market_data/       # Market data providers, cache policy, symbol resolution.
  core/              # Shared models, indicators, backtest engine, reporting primitives.
  strategies/        # Strategy components and strategy group registry.
  research/          # Long-form research workflows and current best strategy notes.
  experiments/       # Ablations, parameter scans, walk-forward comparisons.
  selection/         # Apply fixed strategies across symbols and rank candidates.
  execution/         # Entry decision, position plan, risk plan, future broker adapters.
  viz/               # Backtest charts, strategy matrix dashboards, report renderers.
  commands/          # New command modules and legacy compatibility entry points.
```

Keep the top-level modules as compatibility wrappers during the transition:

- `mu_strategy.data` delegates to `mu_strategy.market_data.cache` / `providers`.
- `mu_strategy.strategy` delegates to `mu_strategy.strategies.registry` / `components`.
- `mu_strategy.walk_forward` delegates to `mu_strategy.experiments.walk_forward`.
- `mu_strategy.visualize` delegates to `mu_strategy.viz.backtest`.
- `mu_strategy.cli` delegates to `mu_strategy.commands.backtest`.

The package names deliberately avoid collisions with existing files such as `data.py`, `strategy.py`, and `cli.py`. This keeps the migration reversible and lets old imports continue to work while internals move.

## Area Boundaries

### 1. Research Area

Purpose: Decide what is currently believed to work and why.

Proposed package:

```text
mu_strategy/research/
  mu_current.py
  cross_asset_notes.py
  reports.py
```

Responsibilities:

- Track the current MU baseline and why it is preferred.
- Summarize known weaknesses, such as low win rate or high drawdown.
- Compare strategy families across venues and symbols.
- Link to generated reports without embedding report generation logic.

This layer can read experiment outputs, but it should not contain backtest mechanics.

### 2. Experiment Area

Purpose: Test strategy ideas against controlled data and compare variants.

Proposed package:

```text
mu_strategy/experiments/
  walk_forward.py
  ablation.py
  parameter_grid.py
  compare.py
```

Responsibilities:

- Run walk-forward backtests.
- Run MU-specific ablations.
- Compare strategy groups across windows.
- Produce experiment result objects and Markdown tables.

This layer can use strategy groups from `strategy/` and candles from `data/`, but it must not choose live trades.

### 3. Fixed-Strategy Execution Area

Purpose: Use a fixed strategy to select symbols, decide whether to enter, and prepare a trade plan.

Proposed package:

```text
mu_strategy/execution/
  decision.py
  plan.py
  risk.py
  paper.py
```

Responsibilities:

- Evaluate the latest confirmed data against a fixed strategy group.
- Output an entry decision: allow, block, or wait.
- Build position plans and stops from strategy rules.
- Later support paper/live execution adapters.

Important boundary: execution must not tune strategy parameters. It consumes an approved strategy.

### 4. Visualization Tool Area

Purpose: Make research and experiments easier to inspect.

Proposed package:

```text
mu_strategy/viz/
  backtest.py
  strategy_matrix.py
  experiment_dashboard.py
```

Responsibilities:

- Render single-strategy HTML backtests.
- Render strategy group component dashboards.
- Render experiment comparison dashboards.

This layer should accept structured result objects and should not fetch data directly.

### 5. Data Area

Purpose: Provide clean, confirmed, reproducible candles.

Proposed package:

```text
mu_strategy/market_data/
  providers/
    binance.py
    okx.py
  cache.py
  symbols.py
  candles.py
```

Responsibilities:

- Fetch candles from Binance and OKX.
- Ignore unconfirmed OKX candles.
- Reprocess the latest cached candle on incremental refresh.
- Resolve requested symbols to venue symbols.
- Return data source metadata with every dataset.

Data code should not know about strategy groups.

### 6. Strategy Area

Purpose: Define strategy components and assemble named groups.

Proposed package:

```text
mu_strategy/strategies/
  components.py
  registry.py
  presets/
    mu.py
    btc.py
```

Responsibilities:

- Define `EntryStrategy`, `PositionStrategy`, `ExitStrategy`, and `FilterSet` metadata.
- Define `StrategyConfig` for executable parameters.
- Register named strategy groups such as `baseline`, `direct_next_open`, and `optimized_v2`.
- Keep component labels aligned with reports.

This layer should be deterministic and testable without network access.

## Data Flow

```text
market_data provider -> confirmed candles -> strategy registry -> experiment runner -> result model -> report/viz
                                  \-> execution decision -> position/risk plan
```

Research reads the outputs of experiments and execution dry runs. It should not fetch raw market data or mutate strategy configs.

## CLI Migration

Keep current commands working:

```powershell
python -m mu_strategy.cli
python -m mu_strategy.walk_forward
python -m mu_strategy.visualize
```

Add clearer long-term commands behind compatibility wrappers:

```powershell
python -m mu_strategy.commands.backtest
python -m mu_strategy.commands.experiment
python -m mu_strategy.commands.select
python -m mu_strategy.commands.decide
```

The old commands should import and call the new modules, then be removed only after reports and tests use the new commands.

## Testing Strategy

Use incremental tests for each migration slice:

1. Data package tests:
   - OKX unconfirmed candles are ignored.
   - Last cached candle is reprocessed.
   - Binance cache path remains compatible.

2. Strategy package tests:
   - All current strategy group names remain available.
   - `second_pullback_limit_8` alias still maps to `baseline`.
   - Component metadata matches the report matrix.

3. Experiment tests:
   - Walk-forward output is unchanged for fixture candles.
   - Strategy group reports include metrics and component matrix.

4. Visualization tests:
   - HTML dashboard contains all expected sections.
   - Rendering consumes result objects instead of fetching data internally.

5. CLI smoke tests:
   - Old commands still run.
   - New command modules run.

## Migration Plan

### Phase 1: Data and Strategy Split

Move data provider/cache logic into `mu_strategy/market_data/`. Move strategy group definitions into `mu_strategy/strategies/`. Keep compatibility wrappers so current imports still work.

### Phase 2: Experiment and Visualization Split

Move walk-forward logic into `mu_strategy/experiments/`. Move HTML rendering into `mu_strategy/viz/`. Keep current `walk_forward.py` and `visualize.py` as CLI wrappers.

### Phase 3: Research and Selection

Create research summaries and fixed-strategy selection workflows. Move cross-asset basket work into `selection/` so it is not mixed with MU-specific experiments.

### Phase 4: Execution Planning

Add non-trading execution planning: latest-data decision, entry/stop/position plan, and risk summary. Do not connect live trading in this phase.

## Key Risks

- Moving too much at once can break reproducibility.
- Report file paths are part of the research workflow; breaking them would make prior results harder to compare.
- Strategy labels can drift from executable parameters if metadata is duplicated.
- Execution planning can accidentally become parameter tuning unless the boundary is enforced.

## Decision

Use the recommended layered architecture with compatibility wrappers. Start with data and strategy split because those are the shared foundation for research, experiments, selection, execution, and visualization.
