# 可信行情：刷新、读取与存储

研究和信号消费可信 generation。日常只需选择刷新范围、发布快照，再运行消费者；内部校验与存储契约集中在本文，研究回放见[研究指南](research-guide.md)。

## 日常刷新

只刷新 MU（可重复传入 `--symbol`）：

```powershell
python -B -m mu_strategy.commands.refresh_market_data --symbol MU-USDT-SWAP --days 180 --data-dir data/live --html-output reports/live/data_health.html
```

显式 symbol 会规范化并去重，不查询 Top universe，也不会因 `--limit` 扩大范围。不传 symbol 时，默认维护 OKX 热门币和本地股票概念代币池各 Top10；周期为 `5m/15m/1h`。

持续使用同一刷新流程可加 `--loop --interval-seconds 300`。日常信号运行也会调度这条唯一写者流程，见[信号服务手册](signal-service.md)；同一数据目录的刷新调度需要统一管理。

刷新输出和健康 HTML 包含 `refresh_segments`、最多 5 条 `slowest_segments`、`failed_segments` 和 `blocking_symbols`。进程退出码、刷新尝试、快照可用性和消费者是否允许读取是不同结果；不能只凭一次 fetch 成功判断数据可用。

### 首次启动与覆盖不足

180 天是请求的滚动历史窗口；合约历史不足时，只保留实际已有数据，并在 manifest 中保留 `partial_available_history` 和实际覆盖天数，不填造上市前 K 线，也不把请求天数改小。如果 180 天窗口已完整，但首个存储月恰好是月中上市的月份，首次完整历史刷新会额外查询 OKX 公共合约信息的 `listTime`：只有首根 5m K 线与上市所在桶一致、15m/1h 起点与完整 5m 比较窗口一致时，才使用已有的 `.partial-<起点时间>.csv` 首月格式。首次发布的对应数据集记录 `verified_listing_start:listing_time_ms=...`，逻辑覆盖仍如实标记 `complete`。

例如 MU 于 2026-03-04 上市，2026-09-06 请求 180 天时，不再因无法取得 3 月 1 日的上市前数据而失败。没有可信上市信息、返回起点晚于应有首根 K 线、时间矛盾或请求失败时仍拒绝发布；不会把接口截断当作上市边界。普通月初回看完整和增量刷新无需额外查询上市时间；重启后继续复用已验证的首月分段。已有 strict gate、数据新鲜度、连续性和 built/native 校验保持不变。

增量复用与首次校验共用上一 generation 的 5m 时间窗口末端。15m/1h 尚未完整形成的尾部 K 线不会移动这个窗口，避免非整点运行误判覆盖不足并丢失首月分段的复用依据。缺少可用的 5m 基准时重新拉取完整历史，不自行放宽覆盖判定。

上市月边界实现已随 [PR #108](https://github.com/amazing-fish/mu-5x-fibonacci-trading/pull/108) 合并；[#107](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/107) 仍开放，不能据此认定代码缺失，也不能仅凭合并认定实际运维验收完成。

## 显式迁移

schema-v3 flat generation 可通过离线迁移进入 v4；替换下列占位符后执行。`--publish` 会发布新 pointer，不能作为普通读取的隐式动作。

```powershell
python -B -m mu_strategy.commands.import_trusted_generation --data-dir data/live --source-run-id <v3_run_id> --target-run-id <new_v4_run_id> --publish
```

## 文件与产物

- `current.json`、对应 manifest 及其引用的 segment 共同构成可信快照；单独复制 pointer 不等于保留数据。
- `reports/live/` 是默认 ignored 产物目录，Markdown/HTML 可重新生成，不是权威 baseline。
- tracked pointer 可能被刷新改变；固定研究应显式选择已保留的 generation，不能用 checkout/reset 恢复旧 pointer。
- 当前没有 retention、pin policy 或 GC，不应假设旧 generation/segment 会被自动删除。

以下是当前实现的存储和发布契约，保留源代码标识以便查阅。

## Filesystem durability

Module: `mu_strategy.fs_durability`

`fsync_directory()` is the single owner of the POSIX directory-fsync and Windows directory-handle `FlushFileBuffers` implementation. Open, flush, close, and unsupported-platform failures raise `OSError`; callers may wrap that failure in their own domain error, but they may not silently claim durability.

This low-level module does not define a publication commit point or recovery protocol. Trusted-generation publication, Stage 0 observation append logs, and immutable strategy-release publication still decide independently which file and directory entries must be durable, in what order, and how a failed or ambiguous write is recovered.

Trusted-generation publication keeps atomic replacement of `current.json` as its commit point. File and directory-sync failures before that replacement still fail publication. If the replacement succeeds but syncing its parent directory fails, the new complete pointer is already visible, so the writer reports `current_pointer_directory_sync_failed` as an explicit publication warning instead of returning a false rollback/failure result. A restart after that warning may observe either the previous complete pointer or the new complete pointer; trusted readers continue to validate whichever generation the pointer names.

## Data

Package: `mu_strategy.market_data`

- `providers/okx.py`: OKX public candle fetching for `MU-USDT-SWAP`.
- `providers/binance.py`: Binance futures candle fetching for explicit comparison runs.
- `cache.py`: CSV cache paths, reads/writes, incremental merge, and lookback pruning.
- `symbols.py`: OKX swap symbol normalization and aliases.
- `universe.py`: dynamic OKX Top USDT-SWAP universe selection.
- `utils.py`: shared candle time helpers, including `infer_candle_interval_ms`, used by reports and experiment windows.
- `trusted_data/contracts.py`: dataclass/Enum contracts for dataset health, validation reports, refresh runs, trust decisions, trusted bundles, and universe snapshots.
- `trusted_data/evaluate.py`: shared publication health classification plus refresh/load candle evaluation for windowing, normalization, freshness, built/native validation, and requested-days coverage.
- `trusted_data/policy.py`: interval dependency planning, freshness policy, and trading/research/observe trust policies.
- `trusted_data/validation.py`: in-memory candle normalization plus `5m -> 15m/1h` built/native validation.
- `trusted_data/store.py`: strict flat-v3/segmented-v4 storage contracts, UTC-month CSV slices, JSON manifests, explicit flat import, and JSONL run-log repository with atomic per-file writes.
- `trusted_data/refresh.py`: the canonical trusted refresh use case; it owns OKX provider calls, ticker universe fetch, shared segment writes, manifest writes, and run-log appends.
- `trusted_data/load.py`: the only trusted cache-only load use case; it never accesses the network and never writes CSV, manifest, or run-log files.
- `trusted.py`: compatibility facade for old public imports; implementation delegates to `trusted_data`.
- `service.py`: thin application facade that adapts legacy `CandleBundle` callers to `trusted_data` refresh/load use cases.

Rules:

- OKX is the default source for MU baseline work.
- Unconfirmed OKX candles are ignored.
- Existing OKX caches are incrementally updated and pruned to the requested `days` window.
- Adjacent candle continuity is gated by `previous close -> next open`; gaps above 2% raise `DataQualityError`.
- A failed refresh does not remove prior published evidence. Current consumers must still pass the current trust policy; an explicitly selected historical generation must pass the historical reader contract. Failure does not grant a cache fallback.
- `data/live/current.json` is the atomic pointer to the current trusted generation. A schema-v4 generation is metadata-only under `data/live/generations/<run_id>/manifest.json` and pins ordered slices of canonical UTC-month CSV files under `data/live/segments/okx/<symbol>/<interval>/<YYYY-MM>.csv`. The global refresh command/use case is the only normal runtime writer for the current pointer, segments, and generation manifests.
- Trusted refresh and trusted consumer load are separate processes. `python -m mu_strategy.commands.refresh_market_data` is the only trusted refresh entry point; backtest, visualization, walk-forward, Fibonacci experiments, and demo are cache-only consumers. `python -m mu_strategy.commands.import_trusted_generation` is an explicit offline migration writer, never an implicit consumer fallback.
- Trusted refresh can be scoped with repeatable `--symbol` values such as `MU` or `MU-USDT-SWAP`. Explicit-symbol mode normalizes and de-dupes OKX swap symbols, skips the Top universe ticker list, and publishes only the requested subset into the same schema-v4 generation contract.
- Trusted refresh may fetch up to `--max-concurrency` symbol/interval segments concurrently (CLI default `2`). Programmatic requests default to serial execution (`1`) so existing compatibility-facade callers with custom fetchers remain thread-safe unless they explicitly opt into concurrency. Complete local and built/native cross-interval validation covers both the exact logical window and the full pre-prune physical lookbehind before any candidate in that symbol bundle can mutate a shared segment; an invalid physical `5m` base also blocks dependent native intervals because their physical equality cannot be established, and parent-interval logical health/hash plus physical storage exclude rows outside the completely covered comparison range. Segment writes, manifest construction, and the single atomic `current.json` publication then remain on the caller thread. Each dataset's shared-tail read/compare/replace is serialized by its stable `.write.lock`, so a stale concurrent refresh re-reads the physical tail after acquiring the lock and cannot truncate a longer prefix. The storage boundary also requires exact interval continuity across every candidate month, including the old-tail/new-suffix join. This narrow writer lock does not cover consumers, the current pointer, unrelated datasets, or store lifecycle.
- Trusted consumers never perform provider/network refresh, CSV writes, segment writes, manifest writes, run-log appends, universe mutation, or canonical `run_id` publication. Backtest, visualization, walk-forward, and Fibonacci experiment entry points default to trusted cache-only loading and no longer accept the old data-path flags `--refresh`, `--source`, or `--trusted-data`; run `python -m mu_strategy.commands.refresh_market_data` first, then run `python -m mu_strategy.cli`, `python -m mu_strategy.visualize`, `python -m mu_strategy.walk_forward`, `python -m mu_strategy.experiments.fibonacci_pullback`, or `python -m mu_strategy.commands.okx_demo_loop`.
- The old in-process per-symbol consumer refresh APIs remain removed. Canonical subset refresh is only available through the standalone trusted refresh command and still writes the shared generation publication.
- Trusted storage is canonical CSV + `current.json` + versioned generation manifests + JSONL run log. It does not use DB, Parquet, or a local web service. UTC calendar month is the pure segment identity. Closed month files are immutable; the trailing month may only append canonical rows while preserving its existing byte prefix. A newly created canonical month must begin exactly one interval after the latest existing canonical predecessor, so it cannot durably close an incomplete prior month. A full-history provider request adds 32 physical lookbehind days while health and manifest coverage retain the exact requested logical `days`; physical rows before the logical start month are discarded. When that fetch fully covers the requested logical window, a missing canonical start month is created only when its first row is the UTC month open; explicitly partial available history does not claim this proof. A verified mid-month listing start uses the partial compatibility path described above, not the canonical month path. The request size alone is never treated as proof that the provider returned complete lookbehind. Newly created `segments/okx/<symbol>/<interval>` directory entries are fsync'd ancestor by ancestor before a referenced file can be published.
- Generated backtest, visualization, data-health, and scanner reports are local artifacts. Write them under ignored paths such as `reports/live/`; do not treat tracked report files as the authoritative baseline.
- Manifest schema v4 records the existing `run_id`, attempt/usability axes, requested/effective intervals, universe snapshot, failures, warnings, diagnostics, and per-dataset health. Its closed `segmented_csv_v1` contract also binds each ordered segment's exact path, `start_row`, row count, timestamp range, content SHA-256, and closed/trailing state. Dataset `content_sha256` remains the hash of the complete logical candle sequence.
- `RefreshAttemptStatus` is refresh-attempt health (`success`, `degraded`, `failed`). Zero usable datasets always classify the attempt as `failed`, regardless of whether the cause was provider failure, cache read failure, validation failure, requested-days coverage, or content hash mismatch.
- `SnapshotUsability` is published snapshot health (`usable`, `stale`, `invalid`) derived from DatasetHealth availability/integrity/freshness. Zero usable snapshots fail closed to `invalid`; mixed usable/unusable snapshots keep the stricter derived dataset state.
- Dataset health is per-cache health: availability, integrity, freshness, reasons, row count, time range, source file, content hash, and validation report.
- Interval dependencies are planned once: `15m` and `1h` consumers automatically include `5m` because built/native validation depends on the base interval.
- Freshness is calculated from clock time, interval length, max staleness bars, and the last confirmed candle timestamp.
- Current and historical readers use the same store contract. A current load reads `current.json` once, pins that generation, and never mixes a later publication; an exact-generation experiment reader never reads `current.json`. A later tail append does not change an older generation because each manifest freezes its selected `[start_row, start_row + rows)` and hashes.
- Schema-v3 generation-local flat CSV remains a strict known read-only layout for exact-ID compatibility. `python -m mu_strategy.commands.import_trusted_generation` is the only v3-to-v4 conversion path. Before any target/shared write it rejects every unusable source dataset and runs the same local and built/native bundle validation; after writing, it verifies candle/hash round trips before optional publication. A normal refresh never incrementally reuses v3 or an imported-v4 snapshot: its first upgrade fetches full month lookbehind. If an import's first month starts after UTC month-open, only that first reference uses a target-run-bound `YYYY-MM.import-<run_id>.csv` compatibility path; later canonical refreshes use `YYYY-MM.csv`, while the old import remains exact-readable. Unknown version/layout/import-path combinations never fall back.
- Missing or malformed manifests/segments, invalid paths, malformed CSV, count/hash/range mismatch, partial claimed data, and historical value corrections fail closed. A correction cannot rewrite stored evidence or replace `current.json`. Retention, pinning policy, GC, and deletion are deferred; this storage layer deletes nothing.

### Storage layout

```text
data/live/
  current.json
  generations/
    <run_id>/
      manifest.json
  segments/
    <source>/
      <symbol>/
        <interval>/
          <YYYY-MM>.csv
  refresh_runs.jsonl
```

Schema-v4 generation directories are metadata-only and contain no CSV. Closed month segments
are immutable and are never rewritten; the trailing month may grow only while preserving its
existing canonical byte prefix. Under continuous refresh, each run adds persistent bytes in
proportion to new candle content plus bounded manifest/run metadata rather than the full retained
history, while total segment residency remains proportional to total retained history. Retention,
pin policy, GC, deletion, and bounding the monotonically growing `refresh_runs.jsonl` are separate
follow-ups. A dataset first published as `partial_available_history` from mid-month uses the
deterministic `YYYY-MM.partial-<physical_first_timestamp_ms>.csv` compatibility path for its first
reference, never the canonical month path. The verified listing-start case also uses this path,
even when its requested logical window is already complete. If incremental appends later make the logical window
complete without proving UTC month-open, subsequent complete generations keep referencing and
growing that same compatibility file. A canonical `YYYY-MM.csv` is created only by a later
full-history response with complete month-open evidence; old compatibility evidence stays exact-readable.
