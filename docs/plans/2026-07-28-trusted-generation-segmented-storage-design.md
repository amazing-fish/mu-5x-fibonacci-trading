---
feature_ids: [R1]
topics: [trusted-data, storage, generation-publication]
doc_kind: design
created: 2026-07-28
issue: 68
---

# Segmented trusted-generation storage design

## Goal and invariants

Continuous trusted refresh must stop copying the complete historical CSV series into every
generation. Canonical CSV remains the authoritative representation, while a generation becomes
a small immutable manifest over shared UTC-calendar-month CSV segments.

The change preserves these existing boundaries:

- `data/live/current.json` remains the sole atomic publication commit point.
- The trusted refresh workflow remains the only normal writer. Backtest, visualization, Demo,
  health rendering, and historical experiment readers remain cache-only.
- A reader pins one generation manifest once and derives every dataset read from that pinned
  snapshot. It never re-reads `current.json` during the operation.
- `CSV_FIELDS`, `Candle.to_csv_row()` / `Candle.from_csv_row()`, and the full logical-series
  `candles_content_sha256()` definition do not change.
- Requested/effective interval dependencies, shared health evaluation, freshness, usability,
  strict path validation, and fail-closed behavior do not move into consumer-specific fallbacks.
- No generation or segment is deleted in this change.

## Measured full-copy amplification

The repository-tracked flat schema-v3 generations were measured directly:

- old: `e702be27d2de4b2d92b12bf01c70d02d`;
- current: `0114f8334f2e40d6ba5b5030faf6007b`.

“Data rows” below excludes the CSV header. “CSV lines” includes one header per file. File sizes
are the exact checked-out byte lengths.

| Interval | Old data rows | Current data rows | New data rows | Old bytes | Current bytes | Added bytes |
|---|---:|---:|---:|---:|---:|---:|
| `5m` | 35,257 | 41,029 | 5,772 | 3,729,190 | 4,345,245 | 616,055 |
| `15m` | 11,752 | 13,676 | 1,924 | 1,247,908 | 1,454,255 | 206,347 |
| `1h` | 2,938 | 3,419 | 481 | 313,849 | 365,765 | 51,916 |
| **Total** | **49,947** | **58,124** | **8,177** | **5,290,947** | **6,165,265** | **874,318** |

Every old data row is an exact ordered prefix of the corresponding current file. The complete
old files, including their headers and line endings, are byte-for-byte prefixes of the current
files. No historical revision was found.

The old generation therefore recopied 49,947 unchanged data rows while adding 8,177 rows. If
headers are counted, the corresponding old-file count is 49,950 CSV lines. The Issue text's
47,950 value is an arithmetic error; this design and the implementation evidence use the
reproducible 49,947-data-row / 49,950-line convention. The redundant share of the current data
rows is 49,947 / 58,124 = 85.94%.

## Selected on-disk representation

Schema v4 uses one shared segment namespace and keeps generation directories metadata-only:

```text
data/live/
  current.json
  generations/
    <run_id>/
      manifest.json
  segments/
    okx/
      <symbol>/
        <interval>/
          <YYYY-MM>.csv
  refresh_runs.jsonl
```

`segment_id_for_open_time_ms(open_time_ms)` is a pure, deterministic total mapping over valid
`Candle.open_time_ms` values. It converts the instant to UTC and returns the zero-padded
Gregorian `YYYY-MM`. It reads no wall clock, locale, provider state, or current generation.
Invalid non-integer or out-of-range timestamps are rejected before path construction.

For each symbol/interval, the v4 manifest contains a closed storage contract:

```json
{
  "schema_version": 4,
  "storage_layout": "segmented_csv_v1",
  "symbols": {
    "MU-USDT-SWAP": {
      "intervals": {
        "5m": {
          "... existing health fields ...": "...",
          "storage": {
            "layout": "segmented_csv_v1",
            "source_root": "segments/okx/MU-USDT-SWAP/5m",
            "segments": [
              {
                "segment_id": "2026-03",
                "source_file": "segments/okx/MU-USDT-SWAP/5m/2026-03.csv",
                "start_row": 0,
                "rows": 8064,
                "first_timestamp_ms": 1772608500000,
                "last_timestamp_ms": 1775027700000,
                "content_sha256": "<lowercase sha256>",
                "closed": true
              }
            ]
          }
        }
      }
    }
  }
}
```

The exact field set is parsed into one shared typed storage contract used by writer, current
reader, exact-generation reader, and migration. Segment references must be ordered by strictly
increasing `segment_id`; IDs and paths must match the dataset key exactly; `start_row` must be
non-negative; rows must be positive;
hashes must be lowercase full SHA-256 values; timestamp bounds must fall inside the named UTC
month; adjacent references may not overlap or reverse time. Every reference except the final
one is `closed: true`; the final reference is `closed: false`.

The existing dataset `content_sha256` remains the hash of the complete logical candle sequence,
not a hash of manifest JSON or concatenated segment hashes. Each segment hash uses the same
canonical candle-row hash algorithm over exactly the referenced contiguous row slice. Dataset rows and
timestamp bounds must equal the aggregate references. A known v3 manifest is a distinct strict
`flat_csv_v1` read-only contract; it is never guessed when v4 fields are absent.

## Closed and trailing segment lifecycle

For a validated dataset, candles are partitioned only by their UTC open-time key. The segment
containing the newest confirmed candle is the sole trailing segment. Every earlier month is
closed because its complete UTC month range is in the past relative to that candle.

A writer may:

1. create a missing segment atomically;
2. acquire the stable dataset-level `.write.lock`, then re-read the physical segment before any
   compare/replace decision;
3. grow the one trailing segment only by appending after every overlapping canonical row has
   matched;
4. extend a formerly trailing segment once more as it becomes closed at month rollover; and
5. reuse an already closed segment without opening it for replacement.

Before creating a canonical month, the writer reads the latest existing canonical predecessor
under the same dataset lock and requires exact one-interval adjacency. A shortened predecessor
therefore cannot be made permanently closed by a later month; after its missing tail is supplied,
the same refresh can append that tail and then create the adjacent month.

The writer renders a growing trailing month to a temporary CSV and atomically replaces the
month file only after verifying that the complete existing file is an exact byte prefix of the
replacement. A generation whose rolling retention window begins inside an existing month records
the corresponding `start_row`; it does not remove the earlier physical rows. This bounds
persistent growth to new segment bytes. Rewriting the bounded open month during staging does
not create another persistent historical copy. Once a reference is closed, subsequent refreshes
must compare it and leave its bytes untouched. The existence of any later valid UTC-month file
in the same dataset directory is the durable physical closure boundary: an earlier file can no
longer grow, even if a pointer is rolled back or an import attempts to extend it.

On a full-history fetch, refresh asks the provider for the requested logical days plus 32 physical
lookbehind days. The shared evaluator still applies the exact requested-days health and logical
window, and separately validates the complete pre-prune physical series for local continuity and
`5m -> 15m/1h` built/native equality before the segment writer receives it as unreferenced
physical material. A physical-only failure marks that dataset invalid and skips its writer hook.
If the physical `5m` base fails, dependent `15m`/`1h` hooks are also blocked because their
built/native relationship cannot be proven from invalid base evidence.
For a valid base, both logical health/hash material and writer input are limited to parent rows
inside the complete `5m` comparison window; independently fetched native edge rows that were not
compared never become canonical.
Thirty-two days are sufficient to cover the complete UTC month containing any logical-window
start, but the request size is not proof that the provider returned those rows. Physical partitions
before the logical start month are discarded. When a full-history result completely covers the
requested logical window, a missing canonical start month is written only when its first candle is
the UTC month open; a shortened response that nevertheless satisfies logical coverage therefore
fails before shared mutation. Explicit `partial_available_history` remains a distinct, visible
state and does not claim this proof. A later wider request can move `start_row` earlier inside a
complete stored month without prepending rows or changing an older manifest's offsets. Extra
provider rows before the already stored physical prefix are ignored; every overlapping timestamp
must still match canonically, and a logical slice outside the stored lookbehind fails closed.

A healthy flat-v3 current generation is readable compatibility evidence, not a safe incremental
base for the first canonical v4 month. Refresh verifies that v3 dataset, then forces a full-history
lookbehind fetch. The same one-time full-fetch rule applies when current is an explicitly imported
v4 snapshot.

The lock is deliberately scoped to one `symbol/interval` writer directory. It serializes only the
shared-tail read/compare/replace sequence, so a stale shorter refresh cannot replace a longer tail
written by another process. It is not a store-wide publication lock, consumer lock, preparation
lease, retention mechanism, or garbage collector.

Any changed value at an already stored timestamp is a historical correction. This design chooses
**fail closed** rather than creating an implicit variant or rewriting evidence. The refresh raises
a typed storage error before manifest/current publication. Handling corrected immutable evidence
would require an explicit future identity/version design; it is not hidden in this PR.

## Exact generation snapshots while the tail grows

Every segment reference freezes:

- its zero-based physical `start_row`;
- its row count;
- its first and last timestamps;
- the canonical hash of exactly those rows; and
- whether that reference was already closed.

A reader opens the pinned manifest's ordered references and reads exactly
`[start_row, start_row + rows)` from each file. It verifies the skipped and selected CSV structure,
the parsed selected candles, month membership, row count, reference
hash, aggregate ordering, aggregate row/timestamp metadata, and full logical-series hash.

An open segment may contain additional rows appended by a later or not-yet-published refresh.
Rows outside the pinned slice are not part of that generation. The selected slice is still
verified against its old hash. A closed reference may leave older physical rows before
`start_row`, but its selected range must reach physical EOF and rejects later extra rows. Thus
generation A remains exactly reproducible after generation B grows the same trailing month or
moves a rolling retention boundary within that month, without copying A's full dataset.

This immutable-slice rule also protects a current reader while refresh is staging. A reader that pinned A
before publication never consults B's manifest and cannot mix A and B interval references.

## Publication, durability, and crash observations

The write order is:

1. validate and materialize all candidate candle bundles through the existing shared evaluator,
   including both logical-window and full physical-lookbehind local validation plus
   `5m -> 15m/1h` built/native comparison, before invoking any segment writer hook;
2. create or atomically grow the required shared segment files under the dataset writer lock and
   fsync every newly created directory ancestor plus the final file/directory state; every month
   must remain exactly interval-contiguous across the existing-tail/new-suffix boundary;
3. write and fsync `generations/<run_id>/manifest.json`;
4. re-read and strictly validate that manifest and all storage references;
5. atomically replace and directory-sync `current.json`;
6. append the audit run log; a post-commit audit failure remains a warning.

`current.json` replacement in step 5 is the commit point.

- Crash/failure before step 5: the old pointer remains authoritative. It may observe longer
  physical open-month files, but reads only its pinned verified slices. New segment files and
  the unreferenced generation manifest are inert staging evidence.
- Crash/failure after step 5: the new generation is authoritative and every referenced slice
  was already durable. A run-log failure cannot roll back publication and is reported as a
  warning, preserving the existing audit semantics.

Atomic full-file replacement of the bounded trailing month preserves every previously referenced
slice while avoiding a torn claimed range. A malformed or partially written unreferenced suffix
cannot be promoted: the next writer
strictly reads the physical segment before reuse and fails closed. If a manifest is corrupted to
claim such a suffix, the reader fails its row/hash/CSV validation. An older manifest whose valid
slice ends before an uncommitted suffix remains readable by construction.

## Strict read and failure semantics

The common dataset reader dispatches only from the parsed schema/layout pair:

- schema v3 + `flat_csv_v1`: exact generation-local `okx/<symbol>/<interval>.csv`;
- schema v4 + `segmented_csv_v1`: exact shared source root and ordered segment references;
- every other version/layout/combination: reject.

There is no “try segmented, then flat” fallback. Missing files, empty available datasets, wrong
CSV headers, malformed rows, invalid UTF-8, path escape, duplicate/out-of-order references,
wrong month membership, wrong closed/trailing flags, count mismatch, segment hash mismatch,
full logical hash mismatch, partially written claimed data, and unknown layouts all fail closed.

Current and historical readers call this same storage reader. The historical reader supplies an
exact run ID and never reads `current.json`; the current reader reads `current.json` once to make
its `TrustedLoadContext`. Neither reader can call a provider or write/import data.

## Explicit flat-v3 import

Existing v3 generations remain immutable and strictly readable by exact ID. Conversion is an
explicit migration operation owned by the trusted refresh/storage writer boundary:

1. accept an exact source v3 run ID and a distinct target v4 run ID;
2. parse the source as schema v3 only and bind its manifest run ID to its directory;
3. before creating the target generation or any shared segment, reject every unusable dataset,
   read exact generation-local paths, and apply the same local plus built/native bundle validation;
   verify rows, timestamps, per-dataset logical hashes, health, and interval dependencies;
4. partition the verified candles through the same UTC key and segment writer used by refresh;
   if the first month begins after UTC month-open, bind only that first reference to
   `YYYY-MM.import-<target_run_id>.csv`, because an offline import has no provider from which to
   obtain the missing physical prefix;
5. construct and strictly verify a new v4 manifest with import provenance;
6. re-read the new exact generation and assert identical `Candle` values and full logical hashes;
7. publish it only when the explicit migration invocation requests publication, using the same
   `current.json` commit point.

Normal readers never import. Refresh never silently interprets malformed v4 as v3. The original
v3 generation and files are retained unchanged. Import compatibility filenames are accepted only
for the first reference of a manifest carrying `imported_from_run_id`, and the embedded target run
ID must exactly equal that manifest's `run_id`. The next ordinary refresh forces full lookbehind
and writes the canonical `YYYY-MM.csv`; it never prepends to or rewrites the import compatibility
file, so old exact replay remains stable.

## Rejected alternatives and deferred lifecycle policy

SQLite remains rejected because it would introduce a second canonical storage/transaction
semantic beside the execution database, make evidence less directly reviewable, and add schema
and migration cost without solving the trusted manifest boundary.

A content-addressed blob pool remains rejected because opaque identities would require
reference-count/pinning rules and garbage collection before safe deletion. It is unnecessary
when calendar segments already match append behavior and operational inspection.

Retention, pin policy, compaction, garbage collection, orphan cleanup, and deletion are a
separate lifecycle issue. This implementation records references but never decides that evidence
is unpinned and never deletes either generations or segments.

## Required verification evidence

Tests must cover:

- UTC month/month-end/month-start/year rollover and timezone-independent partition keys;
- same-month logical-window expansion using physical lookbehind without changing old offsets;
- real multi-cycle refresh directories whose persistent byte delta is new segment bytes plus
  bounded manifests/log metadata, not a new full-history CSV;
- byte-identical closed files and prefix-stable trailing growth;
- cross-process stale-tail serialization and a valid exact generation after each current switch;
- ancestor directory fsync for a first segment write;
- old/current exact-ID replay and full logical hashes after later publication;
- one-operation generation pinning during publication;
- fail-closed historical correction;
- failures before and after `current.json` replacement;
- explicit flat-v3 import and candle/hash round trip;
- missing, empty, malformed, partially written, corrupt, misordered, duplicate, unknown-layout,
  segment-hash, count, and full-hash failures;
- provider spies proving every consumer and historical reader remains cache-only; and
- the existing health, interval-dependency, publication, and release-experiment contracts.

The final verification is the full repository command:

```text
python -m unittest discover -s tests -p "test_*.py" -v
```
