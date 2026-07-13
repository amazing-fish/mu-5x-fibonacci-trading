---
feature_ids: [50]
topics: [research-correctness, market-context, lookahead]
doc_kind: bug-report
created: 2026-07-14
---

# Hourly context close visibility

## Reporter

Codex, during the read-only architecture and research-validity audit that led to Issue #50.

## Diagnosis capsule

| Field | Finding |
|---|---|
| Symptom | A regime computed from a 1h candle close was visible to 15m bars from that candle's open time. |
| Evidence | `build_hourly_context` stored close-derived regimes under `candle.open_time_ms`; the prior behavior test expected that state at the hour open. Release experiments separately shifted 1h timestamps by one hour. |
| Root cause | Temporal availability was assigned at the wrong ownership seam. The core owner used open time, while one caller compensated locally, creating two contracts. |
| Diagnostic strategy | Trace every `build_hourly_context` caller and compare the standard path with the release experiment's causal reference behavior. |
| Timeout strategy | Stop if one owner-level change cannot keep every caller consistent; do not add per-consumer flags or adapters. |
| Warning strategy | Multiple visibility modes, strategy tuning, data refresh, or report regeneration would indicate scope drift. |
| User-visible correction | Standard research and scan results use only completed 1h information. Pre-fix ordinary reports are not directly comparable. |
| Acceptance | Boundary tests prove Red then Green, release experiments apply the boundary exactly once, and the full deterministic suite passes. |

## Reproduction

1. Construct 1h candles opened at `T` and `T+1h` with deterministic green/red regimes.
2. Construct 15m candles at `T`, `T+15m`, `T+1h`, `T+1h15m`, and `T+2h`.
3. On the pre-fix implementation, the first regime appears at `T` and the second at `T+1h`.
4. Expected: yellow before `T+1h`, the first regime from `T+1h`, and the second from `T+2h`.

The focused regression test failed before the implementation change with the expected value mismatch:

```text
python -m unittest \
  tests.test_market_context.MarketContextTests.test_hourly_state_becomes_visible_only_after_candle_close \
  tests.test_market_context.MarketContextTests.test_15m_candles_before_first_hourly_candle_remain_yellow -v

Ran 2 tests ... FAILED (failures=2)
```

## Fix

- Make `mu_strategy.core.market_context` expose each close-derived 1h regime at `open_time_ms + 1h`.
- Pass canonical 1h candle timestamps from release experiments instead of shifting them at the caller.
- Build ordinary walk-forward and monthly Fibonacci contexts once from the complete supplied history, then select each report partition's 15m keys. Reporting partitions no longer discard the last completed regime or restart its indicator history.
- Keep one temporal contract for CLI, visualization, scanner, ordinary walk-forward, and release experiments.

No strategy configuration, trusted data, fill model, Demo mutation, or Production execution behavior is changed.

The pinned release protocol remains an explicit cold-start exception: it excludes pre-window context by contract and does not reuse ordinary report-partition state.

## Review round 1

Cloud review identified that the ordinary walk-forward and monthly Fibonacci callers sliced 1h inputs at each report boundary. After close visibility moved into the core owner, the slice omitted the previously completed regime and reset EMA/MACD history, producing a false initial `yellow` state.

- Red: two behavioral tests compared each partition with the canonical full-history context and failed with `green -> yellow` mismatches (`exit 1`, two failures).
- Green: both callers now reuse the canonical causal context and select their partition's 15m keys; the same two tests pass (`exit 0`).
- Failure-mode sweep: every `build_hourly_context` caller was inspected. These were the only ordinary reporting partitions that rebuilt context from a truncated 1h slice. The release-candidate slice is not changed because its documented protocol requires independent cold starts and rejects pre-window state.
- Fallback analysis: no fallback branch or compatibility mode was added; the repository-specific fallback checker is absent.

## Verification

- Red: the two market-context boundary tests failed before the implementation change with two expected assertion mismatches (`exit 1`).
- Focused: `python -m unittest tests.test_market_context tests.test_strategy_release_candidate.ReleaseExperimentRunnerTests -v` passed all 10 tests (`exit 0`).
- Affected surfaces: `python -m unittest tests.test_market_context tests.test_strategy_release_candidate tests.test_walk_forward tests.test_entry_scanner tests.test_visualize tests.test_backtest -v` passed all 72 tests (`exit 0`).
- Review-affected surfaces: `python -m unittest tests.test_walk_forward tests.test_fibonacci_pullback tests.test_market_context tests.test_strategy_release_candidate -v` passed all 34 tests (`exit 0`).
- Real-path exercise: a deterministic exponential rise followed by an hourly collapse kept the prior `green` state during the open hour and exposed `red` exactly at that candle's close (`exit 0`, no mocks).
- Feature gate: real no-mock walk-forward and Fibonacci horizon runs completed from synthetic candles with one 96-bar walk-forward window and one monthly Fibonacci result (`exit 0`).
- Full suite: `PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 python -m unittest discover -s tests` passed all 469 tests in 8.335 seconds (`exit 0`).
- Diff hygiene: `git diff --check` completed with no errors (`exit 0`).

## Rollback

Revert the code, tests, and documentation together. Rollback does not refresh data, rewrite trusted generations, or call a broker.
