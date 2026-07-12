# Strategy Release Provenance Implementation Plan

> **For AI:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a deterministic, versioned strategy-release evidence contract and one independently reviewed approved MU baseline release that 30-B can resolve by exact ID.

**Architecture:** Separate registry-owned rule identity, self-contained candidate evidence, promotion-time SCM review authenticity, and runtime artifact integrity. Candidate generation runs only from an exact clean implementation commit against an explicit historical trusted generation; the approved release is added in a later commit after an independent review record exists.

**Tech Stack:** Python standard library, frozen dataclasses/Enums, canonical JSON/SHA-256, existing trusted-data manifests/CSV loaders, existing deterministic backtest engine, `unittest`, Git/GitHub CLI only at offline workflow boundaries.

---

### Task 1: Share canonical JSON without changing Stage 0 bytes

**Files:**
- Create: `mu_strategy/canonical.py`
- Modify: `mu_strategy/observations.py`
- Modify: `tests/test_stage0_observations.py`

**Step 1: Write the failing byte-compatibility tests**

Add tests that pin canonical JSON and SHA-256 behavior, including key order, ASCII escaping, compact separators, tuples, and non-finite rejection:

```python
def test_shared_canonical_json_matches_stage0_contract(self):
    payload = {"z": ("é",), "a": 1}
    self.assertEqual('{"a":1,"z":["\\u00e9"]}', canonical_json(payload))
    self.assertEqual(canonical_payload_sha256(payload), canonical_sha256(payload))

def test_shared_canonical_json_rejects_non_finite_numbers(self):
    with self.assertRaises(ValueError):
        canonical_json({"value": float("nan")})
```

**Step 2: Run the tests and verify RED**

Run:

```powershell
python -m unittest tests.test_stage0_observations
```

Expected: import failure for `mu_strategy.canonical`.

**Step 3: Implement the shared helper**

Expose only:

```python
def canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True)

def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
```

Move Stage 0 calls to these helpers and retain `canonical_payload_sha256` as its public compatibility wrapper.

**Step 4: Run Stage 0 focused tests**

Run:

```powershell
python -m unittest tests.test_stage0_observations tests.test_stage0_observation_integration
```

Expected: all existing and new tests pass with identical JSON/fingerprints.

**Step 5: Commit**

```powershell
git add mu_strategy/canonical.py mu_strategy/observations.py tests/test_stage0_observations.py
git commit -m "refactor: share canonical contract serialization"
```

### Task 2: Make rule identity authoritative and freeze executable config v1

**Files:**
- Modify: `mu_strategy/strategies/registry.py`
- Create: `mu_strategy/research/strategy_releases.py`
- Create: `tests/test_strategy_release_provenance.py`

**Step 1: Write RED registry/config tests**

Cover:

- baseline descriptor equals `mu.baseline.second_pullback.long_limit.v1`;
- duplicate `strategy_rule_id` and alias rejection;
- candidate code cannot supply a free-form rule ID;
- config payload contains the exact current `StrategyConfig` field set;
- tuple fields remain ordered lists in JSON and every float becomes a finite canonical decimal string;
- unknown/missing fields fail closed;
- payload round trip recreates the same config SHA.

Use a frozen descriptor:

```python
@dataclass(frozen=True)
class StrategyRuleDescriptor:
    strategy_rule_id: str
    strategy_name: str
    semantic_version: int
    side: str
    order_type: str
```

**Step 2: Run RED tests**

```powershell
python -m unittest tests.test_strategy_release_provenance
```

Expected: descriptor/config contract types missing.

**Step 3: Implement descriptor ownership**

Add `rule: StrategyRuleDescriptor` to `StrategyGroup`. Construct the baseline descriptor in the registry; aliases resolve to the same descriptor rather than inventing another ID. Add a catalog validator used by tests and candidate generation.

**Step 4: Implement frozen config payload**

Define `StrategyConfigPayloadV1` with an exact field list matching the current dataclass. Serialize control floats as normalized decimal strings, tuples as tuples internally/lists on wire, reject bool-as-number and non-finite values, and derive `strategy_config_sha256` solely from `to_dict()`.

Do not use `asdict()` as the strict parser and do not silently absorb future `StrategyConfig` fields. Add a test asserting the owned field set equals the current dataclass field set so a future field addition requires a v1 decision.

**Step 5: Run focused tests and commit**

```powershell
python -m unittest tests.test_strategy_release_provenance tests.test_research_selection
git add mu_strategy/strategies/registry.py mu_strategy/research/strategy_releases.py tests/test_strategy_release_provenance.py
git commit -m "feat: define authoritative strategy rule identity"
```

### Task 3: Implement candidate, protocol, result, approval, and release contracts

**Files:**
- Modify: `mu_strategy/research/strategy_releases.py`
- Modify: `tests/test_strategy_release_provenance.py`

**Step 1: Write the full RED construction matrix**

Add frozen enums/types for:

- `ExperimentWindowRole`: TRAIN, VALIDATION, OUT_OF_SAMPLE;
- `ReleaseDecision`: APPROVED, REJECTED;
- `SelectionReasonCode`: BASELINE_CONTINUITY;
- `FillModel`: DETERMINISTIC_OHLC;
- `PartialFillModel`: NONE;
- `UndefinedMetric`: NOT_DEFINED_ZERO_LOSS only if a stored metric can be undefined;
- `ExperimentWindow`, `BacktestAssumptionsV1`, `ExperimentWindowResultV1`;
- `TrustedExperimentDatasetV1`;
- `StrategyReleaseCandidateV1`, `ScmReviewSnapshotV1`, `StrategyReleaseApprovalV1`, and `StrategyReleaseV1`.

Tests must mutate each control field and prove the appropriate candidate/result/release ID changes. Also test exact fields, unknown schema/enums, duplicate intervals/symbols, invalid Git/hash/ID formats, empty/reversed/overlapping/non-contiguous windows, invalid decimals, bools, NaN/Infinity, result mismatch, approval mismatch, and rejected releases.

**Step 2: Verify RED**

```powershell
python -m unittest tests.test_strategy_release_provenance
```

**Step 3: Implement the closed protocol**

Use `EXPERIMENT_PROTOCOL_ID = "mu.baseline.walk_forward.cold_start.v1"`. Enforce exactly three contiguous `[start_ms, end_ms)` windows in role order, cold-start/no-carry semantics, explicit assumptions, and finite result summaries. Store gross profit/loss rather than Infinity-prone profit factor.

**Step 4: Implement content identities**

- `candidate_fingerprint = sha256(candidate control payload excluding itself)`;
- `approval_snapshot_sha256 = sha256(canonical SCM review snapshot)`;
- `strategy_release_id = "sr1_" + sha256(candidate + approval)`.

The approval must bind the candidate fingerprint and evaluated commit. Runtime parsing verifies snapshot integrity and duplicated fields but does not claim offline reviewer authentication.

**Step 5: Run tests and commit**

```powershell
python -m unittest tests.test_strategy_release_provenance
git add mu_strategy/research/strategy_releases.py tests/test_strategy_release_provenance.py
git commit -m "feat: add versioned strategy release contracts"
```

### Task 4: Add an explicit historical-generation reader

**Files:**
- Create: `mu_strategy/experiments/release_candidate.py`
- Create: `tests/test_strategy_release_candidate.py`

**Step 1: Write RED historical reader tests**

Use temporary copied fixtures and prove:

- an explicit generation reads without `current.json`;
- changing/removing `current.json` does not affect the result;
- old published freshness is retained rather than recalculated from the test clock;
- directory run ID must equal manifest run ID;
- schema v3, usability/integrity, requested/effective intervals, contained relative paths, CSV presence, and every content SHA are validated;
- path traversal, absolute source files, missing/extra intervals, corrupt CSV, unknown schema, and mismatched hashes fail closed;
- no refresh/provider/network function is called.

**Step 2: Verify RED**

```powershell
python -m unittest tests.test_strategy_release_candidate
```

**Step 3: Implement `HistoricalTrustedGenerationReader`**

The constructor accepts the generation root and explicit run ID. It reads `generations/<run_id>/manifest.json` directly, validates identity and content, and returns immutable candle bundles/data references. It must not import or call the current-pointer `LoadTrustedBundle` orchestration.

**Step 4: Run tests and commit**

```powershell
python -m unittest tests.test_strategy_release_candidate
git add mu_strategy/experiments/release_candidate.py tests/test_strategy_release_candidate.py
git commit -m "feat: read pinned historical experiment generations"
```

### Task 5: Implement the deterministic v1 experiment runner

**Files:**
- Modify: `mu_strategy/experiments/release_candidate.py`
- Modify: `tests/test_strategy_release_candidate.py`

**Step 1: Write RED protocol behavior tests**

Test:

- exact `[start,end)` candle selection;
- cold-start state and no entry/position/pending-state carry between windows;
- deterministic `end_of_data` close;
- exact starting equity/fee/fill assumptions;
- stable decimal quantization and result fingerprint;
- zero-loss windows never serialize Infinity;
- identical reruns produce byte-identical result payloads;
- boundary/assumption changes produce a different candidate fingerprint.

**Step 2: Verify RED**

```powershell
python -m unittest tests.test_strategy_release_candidate
```

**Step 3: Implement runner**

Build hourly context from the exact window input, call the existing backtest engine independently for each split, and convert results into the frozen summary. Do not change strategy/backtest behavior or add parameter selection.

**Step 4: Run tests and commit**

```powershell
python -m unittest tests.test_strategy_release_candidate tests.test_walk_forward
git add mu_strategy/experiments/release_candidate.py tests/test_strategy_release_candidate.py
git commit -m "feat: build deterministic release experiment evidence"
```

### Task 6: Add exact-commit candidate generation

**Files:**
- Create: `mu_strategy/commands/build_strategy_release_candidate.py`
- Modify: `mu_strategy/experiments/release_candidate.py`
- Modify: `tests/test_strategy_release_candidate.py`
- Modify: `.gitignore`

**Step 1: Write RED exact-Git-state and safety tests**

Inject a `GitState` provider and prove:

- dirty worktree rejects generation;
- current HEAD must equal explicit `evaluated_code_commit_sha` exactly;
- ancestor-only match rejects;
- descriptor/config are resolved from the current exact checkout, not CLI strings;
- output defaults under ignored `data/strategy-release-candidates/`;
- current pointer, refresh, provider, private API, leverage, submit, and cancel functions have zero calls.

**Step 2: Verify RED**

```powershell
python -m unittest tests.test_strategy_release_candidate
```

**Step 3: Implement the command**

The CLI takes explicit run ID, symbol, exact evaluated SHA, window boundaries, and output path. It validates exact clean HEAD, resolves the registered baseline descriptor/config, reads the explicit generation, runs the closed protocol, and atomically writes canonical candidate JSON. It never promotes.

Add `data/strategy-release-candidates/` to `.gitignore`.

**Step 4: Run tests and commit the implementation checkpoint**

```powershell
python -m unittest tests.test_strategy_release_provenance tests.test_strategy_release_candidate
git add .gitignore mu_strategy/commands/build_strategy_release_candidate.py mu_strategy/experiments/release_candidate.py tests/test_strategy_release_candidate.py
git commit -m "feat: generate strategy release candidates"
git status --short
git rev-parse HEAD
```

Expected: clean tree. Record this SHA as `IMPLEMENTATION_SHA`; it is the only permitted evaluated SHA for the first candidate.

### Task 7: Generate and independently review the MU candidate

**Files:**
- Generate ignored: `data/strategy-release-candidates/<candidate_fingerprint>.json`
- No tracked code change until review evidence exists.

**Step 1: Derive real split boundaries**

Use the tracked `e702be27d2de4b2d92b12bf01c70d02d` 15m range. Choose explicit contiguous TRAIN/VALIDATION/OOS boundaries inside the available range and record them in the command. Do not infer “latest” data.

**Step 2: Generate from the exact clean implementation SHA**

```powershell
python -m mu_strategy.commands.build_strategy_release_candidate `
  --run-id e702be27d2de4b2d92b12bf01c70d02d `
  --symbol MU-USDT-SWAP `
  --evaluated-code-commit-sha $IMPLEMENTATION_SHA `
  --train-start-ms <exact> --train-end-ms <exact> `
  --validation-end-ms <exact> --oos-end-ms <exact>
```

Expected: one ignored canonical candidate file and a printed candidate fingerprint; `git status --short` remains empty.

**Step 3: Re-run and prove equality**

Generate to a second temporary ignored path and compare SHA-256/bytes. Expected: identical.

**Step 4: Request independent evidence review**

The reviewer must inspect the exact candidate fingerprint, `IMPLEMENTATION_SHA`, config payload, data hashes, protocol, assumptions, and all three result summaries. It must issue an explicit approve/reject statement naming both identities.

If no independent immutable review record is available, stop here, keep Issue #45 open, and do not create an approved artifact.

### Task 8: Implement promotion-time SCM verification and runtime resolver

**Files:**
- Modify: `mu_strategy/research/strategy_releases.py`
- Create: `mu_strategy/commands/promote_strategy_release.py`
- Modify: `tests/test_strategy_release_provenance.py`

**Step 1: Write RED promotion/resolver tests**

Using an injected SCM review provider, cover:

- reviewer differs from candidate/release author;
- live review names exact candidate and implementation SHA;
- edited/deleted/mismatched live record rejects snapshot capture;
- canonical snapshot hash is locally reproducible;
- resolver validates `sr1_[0-9a-f]{64}` before path construction;
- `expected_rule_id` and `expected_symbol` are required and mismatches fail;
- resolver has no by-name/latest/current-pointer overload;
- later checkout HEAD and current registry changes do not affect self-contained release resolution;
- corrupt/unknown/rejected/path-mismatched artifacts fail closed.

**Step 2: Verify RED**

```powershell
python -m unittest tests.test_strategy_release_provenance
```

**Step 3: Implement promotion and resolver**

The promotion command/provider verifies the live independent SCM record and captures its canonical snapshot. It combines only the unchanged reviewed candidate with that snapshot and writes `config/strategy-releases/<release_id>.json` atomically. Runtime resolver checks artifact integrity and caller expectations only; it performs no Git/SCM/network lookup.

**Step 4: Run tests and commit code (not fabricated approval)**

```powershell
python -m unittest tests.test_strategy_release_provenance
git add mu_strategy/research/strategy_releases.py mu_strategy/commands/promote_strategy_release.py tests/test_strategy_release_provenance.py
git commit -m "feat: verify and resolve approved strategy releases"
```

### Task 9: Materialize the reviewed approved release

**Files:**
- Create after verified review only: `config/strategy-releases/<strategy_release_id>.json`
- Test: `tests/test_strategy_release_provenance.py`

**Step 1: Promote using the verified review record**

Run the promotion command with exact candidate and SCM review coordinates. Expected: one tracked canonical approved-release artifact.

**Step 2: Add an artifact-specific regression test**

Resolve the tracked artifact by exact ID with required baseline rule ID and `MU-USDT-SWAP`; assert its candidate fingerprint, evaluated SHA, data hashes, config payload, protocol, and approval snapshot.

**Step 3: Verify and commit**

```powershell
python -m unittest tests.test_strategy_release_provenance tests.test_strategy_release_candidate
git add config/strategy-releases tests/test_strategy_release_provenance.py
git commit -m "data: approve reproducible MU strategy release"
```

### Task 10: Document the boundary and run the complete gate

**Files:**
- Create: `docs/strategy-release-provenance.md`
- Modify: `README.md`
- Modify: `docs/architecture.md`

**Step 1: Document current truth**

Explain rule/config/release identity, historical generation reads, experiment protocol, candidate/review/promotion flow, strict exact-ID resolver, no-current-pointer rule, approval authenticity vs runtime integrity, rollback, and the 30-B handoff. State that approval authorizes only future staged dry-run intent construction and no Broker effect.

**Step 2: Run focused verification**

```powershell
python -m unittest tests.test_strategy_release_provenance tests.test_strategy_release_candidate tests.test_stage0_observations tests.test_stage0_observation_integration
```

Expected: all pass.

**Step 3: Run full verification**

```powershell
python -m unittest discover -s tests
python -m compileall -q mu_strategy tests
git diff --check
git status --short
```

Expected: full suite passes, compile/diff exit 0, and only intended tracked files remain before the docs commit.

**Step 4: Run static safety scans**

Inspect added lines for credentials, private headers, `OrderIntent`, Broker request preparation, leverage/submit/cancel calls, trusted refresh/publication, dynamic tuning, current-pointer reads in the historical reader, and tracked generated reports. Any reachable mutation or fallback is a blocker.

**Step 5: Commit docs**

```powershell
git add README.md docs/architecture.md docs/strategy-release-provenance.md
git commit -m "docs: document approved strategy release provenance"
```

### Task 11: Quality gate, PR, review loop, and merge

**Files:**
- No new scope unless review finds a contract defect.

**Step 1: Re-run the entire gate on final HEAD**

Run focused/full/compile/diff/status again and record exact counts/SHA.

**Step 2: Push and open the Issue #45 PR**

Use the repository PR structure: Summary, Current gap, Design, Safety, Tests, Verification, Risk, Non-goals, Rollback, and `Closes #45`. State that remote checks are absent if they remain absent.

**Step 3: Run quality/fresh-context/independent review**

No author self-review. Address P1/P2 with RED tests and full verification.

**Step 4: Run the mandatory fresh 10-minute quiet window**

Trigger remote review once for each new final head, wait at least 600 seconds, then recheck issue comments, inline comments, reviews, unresolved threads, requested changes, head, mergeability, behind status, checks/workflows, and Issue AC.

**Step 5: Merge using recent repository convention**

Only after every gate passes, merge with the recent merge-commit convention, record the real merge SHA, confirm Issue #45 closes, update/fetch default safely, clean the branch/worktree, then immediately resume the 30-B preflight using the approved release ID.
