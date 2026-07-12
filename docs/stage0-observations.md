---
feature_ids: [30-A]
topics: [execution-roadmap, stage-0, observation, trusted-data]
doc_kind: contract
created: 2026-07-12
---

# Stage 0 observation contract

The Stage 0 observation log is durable evidence of what the strict trusted-data gate and typed scanner observed. It is not an `OrderIntent`, confirmation, broker request, mutation reservation, idempotency ledger, or authorization to call a broker.

The dry-run demo command enables this sidecar by default:

```powershell
python -m mu_strategy.commands.okx_demo_loop --once --dry-run
```

The default local path is `data/observations/stage0.jsonl`. Use `--observation-log <path>` to select another ignored local path. The option is rejected with confirmed Demo orders because 30-A owns only the Stage 0 dry-run boundary.

## Outcomes

Each symbol in a persisted cycle has exactly one closed `ObservationOutcome`:

| Outcome | Meaning | Scanner called? | Later-stage authorization? |
|---|---|---:|---:|
| `DATA_GATE_BLOCKED` | The canonical trusted gate denied the dependency set, trusted loading failed, provenance was incomplete, or a typed input-stage block was returned. | No for trusted loading/gate failures. | No |
| `SCAN_FAILED` | Trusted data was allowed, but the scanner raised or returned an invalid/`UNKNOWN` typed result. | Yes | No |
| `NORMAL_NO_ACTION` | A typed `WAIT`, or a typed `BLOCK` at signal, pending-entry, or execution stage. | Yes | No |
| `READY_FOR_REVIEW` | A typed `READY` result suitable only for human observation. | Yes | No |

Free-text `action`, `reason`, and exception messages are diagnostic fields. Outcome selection uses `TrustDecision.allowed`, `EntryDecisionCode`, `EntryDisposition`, `EntryDecisionStage`, and typed failure codes.

Successful scan evidence is valid only when the trusted gate allowed scanning. Typed trusted-data failures carry only the canonical `MARKET_DATA_UNAVAILABLE / BLOCK / INPUT` metadata shape, so a denied gate cannot be paired with READY or other scanner evidence.

## Version 1 envelope

Each immutable observation records:

- schema, observation, and cycle identity;
- created and trusted-observed timestamps;
- symbol and compatibility source;
- trusted `run_id`, requested intervals, effective dependency intervals, and content SHA-256 for every effective interval when trust is allowed;
- trusted policy name (`trading_strict`), observation policy-contract version, allowed flag, and typed health reason;
- strategy name and canonical strategy-config SHA-256;
- typed decision code, disposition, and stage when a scan result exists;
- sanitized compatibility action/reason;
- canonical trusted-generation provenance;
- the full typed scan result or a typed failure with sanitized exception fields;
- a deterministic result fingerprint and one closed outcome.

Allowed observations require a matching manifest `run_id`/generation identity and complete `5m/15m/1h` dependency hashes. Plain or legacy custom bundles can continue through the versionless compatibility path when the sidecar is disabled, but they are rejected as Stage 0 promotion evidence when versioned persistence is enabled.

## Canonical identity and sanitization

JSON is serialized with sorted keys, compact separators, ASCII escaping, and non-finite numbers disabled. The result fingerprint binds the trusted generation and hashes, policy result, strategy/config identity, typed decision/outcome, typed failure, full numeric scan result, and provenance.

The fingerprint deliberately excludes observation/cycle IDs, timestamps, compatibility source/action/reason, and exception text. Wording or presentation changes therefore do not create a different control identity. Diagnostic text is single-line, limited to 512 characters, and redacts common API key, secret, passphrase, authorization, and signature assignments before persistence.

## Cycle commit and failure semantics

One JSONL line contains one complete `Stage0ObservationCycle` and all of its symbol observations. Before append, the repository persists a transient `<log>.invalid` marker containing the cycle ID. It appends the canonical cycle line, flushes it, calls `fsync`, and only then removes the marker before `run_once` can return the legacy payload or produce a dry-run order plan. Marker creation/removal and first log-file creation also flush the parent-directory entry on POSIX and Windows. On first use, the marker flush first makes the marker entry durable inside the newly created observation directory. The repository then makes the new directory chain discoverable after restart by flushing the directory that contains each newly created directory, from the deepest entry through the first pre-existing ancestor, before appending the cycle. If a newly created directory entry cannot be flushed, the marker remains fail-closed and no cycle line is written. If the marker-removal directory flush fails, the repository restores the marker and reports a failed write.

This cycle-sized commit boundary prevents a later symbol write failure from exposing an earlier subset as a complete cycle. A short or interrupted write can leave only a corrupted trailing line. The strict reader rejects:

- unknown schema versions;
- missing or unknown fields;
- invalid enum/value combinations;
- decision disposition/stage metadata that disagrees with the authoritative decision-code catalog;
- non-numeric or non-finite typed scan fields;
- allowed records without complete run/hash provenance;
- result fingerprints that do not match canonical content;
- malformed UTF-8/JSON and incomplete trailing lines.

Any corrupt line or unresolved failed-write marker makes the repository read fail closed; corrupt, partial, or ambiguously durable data is never returned as promotion evidence. Any marker, append, flush, `fsync`, or marker-removal failure raises an invalid-cycle error, and the current cycle returns no consumable observation or legacy result. Recovery requires explicit inspection/repair; a later append will not silently clear an unresolved marker.

## Compatibility and safety boundary

The existing versionless stdout/dashboard JSON is assembled independently and keeps its exact keys and values. Observation enums, decision metadata, schema fields, and fingerprints do not leak into it.

The Stage 0 integration runs only with `DemoTradingConfig.dry_run=True`. It does not require private credentials, refresh trusted data, publish a canonical generation, create an intent, set leverage, submit an order, or cancel an order. Public instrument metadata reads used by the existing dry-run sizing path remain outside the observation authorization model.
