---
feature_ids: [R0, issue-45, issue-49, issue-69]
topics: [strategy-release, provenance, reproducibility, durable-publication, trading-safety]
doc_kind: spec
created: 2026-07-12
---

# Strategy release provenance

R0 separates a named research strategy from an execution-eligible strategy release. A registry name such as `baseline` is convenient for research, but it does not prove which rule, config, code, data, experiment, or approval was used. Staged execution must consume a content-addressed approved release instead.

## Identities

- `strategy_rule_id` is the registry-owned semantic rule identity. The MU baseline is `mu.baseline.second_pullback.long_limit.v1`.
- `strategy_config_sha256` binds every field in the frozen v1 `StrategyConfig` payload. Canonical serialization rejects missing, extra, non-canonical, and non-finite values.
- `candidate_fingerprint` binds the rule, full config, exact evaluated Git SHA, pinned trusted generation and interval hashes, experiment windows, assumptions, and result summaries.
- `strategy_release_id` is `sr1_` plus the SHA-256 of the unchanged candidate and a verified approval snapshot. The snapshot includes its closed `approval_mode`, so changing only the mode changes both `snapshot_sha256` and `strategy_release_id`.

These identities are orthogonal. Registry membership is not approval, a config hash is not code provenance, and a positive backtest result is not broker authorization.

R0 v1 is closed to `mu.baseline.second_pullback.long_limit.v1` / `baseline` on `MU-USDT-SWAP`. Parsing requires `entry_execution=second_pullback`, base `stop_tightening=baseline`, and no yellow/green stop override, matching the registry-owned baseline semantic shape. Numeric settings such as lookbacks, tolerances, fees, and wait bars may still produce a different config-addressed candidate for independent review; they are not collapsed into the rule ID. A future strategy rule or symbol needs an explicit versioned release-domain binding rather than reusing this identity with different executable semantics.

## Historical experiment protocol

`mu.baseline.walk_forward.cold_start.v1` reads an explicit `data/live/generations/<run_id>/manifest.json`. It never reads `current.json`, recalculates historical freshness from wall-clock time, refreshes data, or calls a provider. The reader validates schema v3 identity, source containment, row counts, and the canonical content SHA-256 of every effective interval.

TRAIN, VALIDATION, and OUT_OF_SAMPLE are explicit contiguous `[start_ms, end_ms)` windows with `input_start_ms == start_ms`. Each window starts with fresh indicator, position, pending-signal, and equity state. Before context construction, its `15m` and source `1h` open times must exactly match the fixed-step expected sets; late starts, early ends, gaps, duplicates, and partial boundaries fail closed. Complete 1h candles become visible at close rather than open, so missing-hour carry, pre-window state, and future hourly closes cannot leak into the split. The existing deterministic OHLC backtest closes any remaining position with `end_of_data`. The candidate records starting equity, fee profile/rate, fill model, slippage, and partial-fill model; result summaries use canonical finite decimal strings. Runner and parser use one closed validator: fee profiles are restricted to the executable domain, config and assumption fees must match, slippage must be zero, schema-v3 effective evidence must close over `5m`/`15m`/`1h`, and every window result must use the declared starting equity. Result parsing verifies return/equity/P&L/zero-trade arithmetic within `0.00000000001`; nested schema versions require real integers. Recomputing fingerprints cannot legitimize a cross-field mismatch.

The first locally reviewed MU candidate uses:

- candidate fingerprint `e9eb5a07017565a1d62f21c453c7bbfb7bfa885c92c4b340c713332ecb63f648`;
- evaluated implementation SHA `b92985b2e9709bcd95effb84e77e7975f916c620`;
- trusted generation `e702be27d2de4b2d92b12bf01c70d02d`;
- tracked `retention-pin.json` for that generation, which keeps refresh reclamation from deleting the referenced evidence;
- 7,048 / 2,352 / 2,348 `15m` candles in three hour-aligned cold-start windows;
- byte-identical repeated candidate output.

An independent local evidence review found no P0-P3 issue, but that verdict is deliberately not sufficient for promotion. There is no tracked approved release until a live immutable SCM review passes the promotion gate.

## Candidate, review, and promotion

Candidate generation requires a clean worktree whose `HEAD` exactly equals the caller-supplied `evaluated_code_commit_sha`. An ancestor match is insufficient. The command resolves the baseline rule and config from that checkout, reads only the explicit generation, runs the closed protocol, and atomically writes canonical JSON under ignored `data/strategy-release-candidates/`.

```powershell
python -m mu_strategy.commands.build_strategy_release_candidate `
  --run-id <run-id> --symbol MU-USDT-SWAP `
  --evaluated-code-commit-sha <exact-clean-head> `
  --train-start-ms <ms> --train-end-ms <ms> `
  --validation-end-ms <ms> --oos-end-ms <ms>
```

`--output` remains available. A repository-contained output uses the repository root as its stable publication boundary. Every repository-external candidate output must also pass `--publication-durability-anchor <existing-ancestor>`; current path existence is never treated as proof that another publisher made the directory entry durable. A nested release directory derives a deterministic existing common ancestor from the already readable candidate path, so its prior path-creation behavior remains available. Candidate and release paths that do not share a suitable writable ancestor can supply the same explicit option without changing the requested final path. The explicit anchor must be supplied again with `--recover-publication`.

Promotion queries the trusted live PR, evaluated commit identity, and review record. The PR must contain the evaluated commit, and missing PR or commit identity fails closed. The required `approval_mode` is recorded inside the immutable review snapshot:

- `independent_review_v1` requires the reviewer to differ from the PR author and the evaluated commit's mapped GitHub author and committer. This is the CLI default.
- `solo_maintainer_v1` permits the reviewer to match the evaluated commit's mapped author and committer. It must be selected explicitly with `--approval-mode solo_maintainer_v1` and carries no independent-review guarantee. GitHub does not permit a PR author to approve their own PR, so the attainable single-maintainer workflow requires a separately authenticated automation or bot actor to open the PR while the maintainer submits the canonical `APPROVED` review.

Solo mode relaxes only reviewer separation from the evaluated commit author/committer. Under both modes, the reviewer must differ from the PR author, the live decision must be `APPROVED`, and the review body must equal these canonical bytes with the exact candidate and evaluated SHA:

```text
APPROVED_STRATEGY_RELEASE_V1
candidate_fingerprint=<64 lowercase hex>
evaluated_code_commit_sha=<40 lowercase hex>
```

Missing, deleted, dismissed, or mismatched review evidence fails closed; self-authored evidence also fails under `independent_review_v1`. Promotion reads the authoritative GraphQL review node and rejects either `lastEditedAt != null` or `includesCreatedEdit=true`, so any edited summary cannot be presented as the original approval bytes. Only after live verification does promotion capture the SCM coordinates, explicit mode, and canonical snapshot, construct the content-addressed release, and atomically write `config/strategy-releases/<strategy_release_id>.json`.

Authenticity and integrity remain separate. Promotion establishes authenticity by querying SCM and applying the recorded approval mode. Runtime does not contact SCM; it accepts only the closed `github` / `amazing-fish/mu-5x-fibonacci-trading` evidence contract and one of the two explicit modes, requires a positive numeric review ID and an exact URL that binds the repository, PR, and review record, applies the captured reviewer/author independence check only for `independent_review_v1`, verifies the canonical three-line statement, and then checks the embedded snapshot digest and all duplicated bindings. A digest alone is not treated as proof of reviewer identity, reviewer independence, or live record existence.

## Durable publication and recovery

Candidate and approved-release writers share the strategy-domain publisher in `mu_strategy.research.strategy_artifact_publication`; neither command owns a private atomic-write clone. Publication is immutable and content-addressed: an identical artifact at the final path is an idempotent success, while different bytes at that path are a typed conflict and are never overwritten.

The authoritative publisher and recovery API require an explicit existing durability anchor that contains the artifact path; they may create parent directories only below that boundary. Default repository-contained candidate publication uses the repository root, every external candidate output requires a caller-selected existing anchor, and promotion deterministically derives an existing common ancestor of the candidate and release paths unless the caller supplies a narrower anchor. The publisher writes and file-syncs a unique temporary file, then creates and file-syncs `.<filename>.publication-pending`. If that exclusive pending write or file fsync fails before final installation, it removes only the directory entry still bound to its own opened inode and syncs the parent, so a partial record cannot become an unrecoverable barrier. After syncing the complete pending record's directory, every publisher syncs each parent-directory entry from the artifact directory back to the stable anchor before checking a concurrent witness or returning success. Therefore an observer that found directories created by a paused publisher still proves the complete lineage durable itself. It then atomically links the complete temporary inode at the final path with no-overwrite semantics, removes the linked temporary name, and only accepts the final inode after the following directory fsync proves both entry changes durable. That linked-name retirement is mandatory because otherwise the temporary alias could mutate the supposedly immutable final; cleanup of independent leftover UUID temporaries remains best effort after fail-closed state exists. An existing or concurrently appearing final path is idempotent only when `lstat`, opened-handle identity, single-link identity, exact-byte comparison, and fsync prove one stable regular file; a symlink, special file, or multiply linked inode is never adopted. This closes the check/install, mutable-symlink, and legacy hard-link alias regressions. After syncing the final directory, the publisher hard-links the already durable publication record as `.<filename>.publication-committed` and syncs that witness entry; this is the authoritative artifact commit point. The pending record remains the read barrier while that witness may only be transiently visible. The ordinary success path removes the barrier and directory-syncs the removal.

Promotion and `StrictStrategyReleaseResolver` check publication state before and after reading. Any pending directory entry blocks consumption, including the interval where a newly linked witness is visible but its directory fsync has not completed. Sidecars and final artifacts are inspected with `lstat` and must be stable regular files; comparison and file fsync use the same identity-checked open handle, including an existing witness or final encountered during no-overwrite installation. Final artifacts must also have exactly one hard link at every identity check. Symlinks (including dangling links), directories, special files, multiply linked files, unreadable metadata, and identity changes during read or fsync fail closed rather than falling through to legacy compatibility. A commit witness becomes readable only after the pending barrier is no longer visible; it must parse under the closed publication-record schema and bind the exact final-byte SHA-256. Files with neither sidecar remain readable for compatibility only as single-link regular legacy JSON artifacts. After a publisher durably acquires pending, it rechecks for a concurrent authoritative witness: matching committed bytes make the operation idempotent even when the two writers recorded different created-parent lineage, while conflicting committed bytes cause the writer to retire its own pending barrier before raising conflict. A failed pre-commit publication cannot fall through to legacy shape because pending is durable before final installation. A failed post-commit barrier removal first restores pending from the witness, using a hard link and then an exact file-sync'd copy fallback. If both restoration paths fail while pending remains absent, the publisher makes one direct parent-directory fsync attempt to prove deletion durable. When that proof also fails, it does not report a false publication failure: the exact final file and witness already crossed the commit point, the currently visible state is committed, and a restart can only retain that state or conservatively resurrect the matching pending barrier for explicit recovery. If any restoration leaves pending visible, the command raises and readers remain blocked. Corrupt, mismatched, or changing sidecars fail closed.

Recovery is explicit. After proving that the previous writer has stopped, rerun the exact candidate-build or promotion command with `--recover-publication`. Recovery requires the recomputed canonical bytes to match the SHA-256 bound by the pending record, validates the recorded created-parent count, and rejects any anchor inside that recorded chain. It then re-syncs the record and its directory plus the complete directory-entry chain to the stable anchor before any final-path action. Recovery either installs or verifies the exact final file before establishing or re-syncing the committed witness and durably removing the pending read barrier. A missing, unrelated, too-narrow, or non-directory anchor, a malformed record, a record for different bytes, or conflicting final content remains fail-closed and requires investigation rather than deletion or overwrite. Ordinary publication never silently adopts pending state.

The final-file and committed-witness installs require same-directory hard-link support, available on the supported NTFS and ordinary POSIX filesystems. An unsupported filesystem raises a typed publication error and leaves pending state; there is no fallback to an overwriting replace.

## Runtime resolution and rollback

`StrictStrategyReleaseResolver.resolve(strategy_release_id, *, expected_rule_id, expected_symbol, required_approval_mode=None)` validates `sr1_[0-9a-f]{64}` before path construction and reads exactly one committed artifact. Candidate construction binds the full config payload symbol to both the dataset and supported-symbol identity and revalidates the protocol cross-field relationships above. Resolution rejects pending-only or sidecar-mismatched publication, missing, malformed, corrupt, rejected, unknown-or-missing-mode, mode-policy-mismatched, untrusted-SCM, non-independent-under-independent-mode, non-canonical-statement, review-coordinate-mismatched, protocol-inconsistent, path-mismatched, rule-mismatched, or symbol-mismatched content. A caller that requires reviewer independence passes `required_approval_mode=ReleaseApprovalMode.INDEPENDENT_REVIEW_V1`; this checks the stored evidence without rewriting it. There is no strategy-name, newest-file, temporary-file, or mutable current-pointer lookup. Later checkout or registry changes do not reinterpret a self-contained release.

Rollback removes the R0 release artifact or code change; it does not change trusted generations, observations, account state, leverage, orders, or positions. Candidate generation and runtime resolution have no network or Broker path.

## 30-B consumption boundary

`OrderIntentFactory` constructs Demo-only staged review data only when the caller supplies an explicit approved `strategy_release_id` and `StrictStrategyReleaseResolver` succeeds for the expected MU rule and exact observation symbol. It then requires the resolved ID, strategy name, and configuration identity to match the canonical Stage 0 evidence. Stage 0 and release configuration hashes use different frozen encodings: the factory decodes the approved release configuration, recomputes the Stage 0 `canonical_payload_sha256(StrategyConfig)` identity for comparison, and stores the release-native schema-wrapped hash in the intent. There is no name, newest-file, current-pointer, legacy/plain, or missing-approval fallback.

The exact release ID is part of the intent fingerprint alongside the source observation, trusted generation/hashes, typed decision, exact rounded order/risk values, the release-derived `second_pullback_wait_bars`, and scanner-derived expiry. Strict intent readers recompute the exclusive `signal_time_ms + second_pullback_wait_bars * 15m` boundary, so readdressing an artifact cannot invent freshness. Approval does not authorize Demo or Production mutation, does not prepare an order request, and does not close [Issue #7](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/7). No actual release is published by this contract change; first publication is tracked by Issue #70, and unit fixtures do not constitute a real acceptance packet.
