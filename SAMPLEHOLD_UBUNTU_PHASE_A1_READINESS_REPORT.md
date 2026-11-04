# SampleHold Multi-Type V2 — Phase A.1 readiness audit

Status: `SAMPLEHOLD_UBUNTU_PHASE_B_CANARY_BLOCKED`  
Baseline: `a69a6af3ebf76d28d3ece8a5753535739c2d763d` (`multi-type-v2/samplehold-phase1`)  
Scope: offline inspection only; physical attempts = **0**. No implementation source, frozen fixture, D0, Pilot result, approval, or Batch Runner was modified. This turn added this report and `tests/test_sample_hold_ubuntu_phase_a1.py`, and isolated one existing import-side-effect assertion in `tests/test_sample_hold_offline.py`.

## Blockers, separated by cause

| Class | Evidence and precise cause | Impact / narrow resolution |
| --- | --- | --- |
| Missing Linux execution binding | `ActiveLearning/main.py:238-244,300-309` intentionally rejects SampleHold D0 and AL. `runtime_provider._sample_hold_evaluator_factory` injects only an explicitly supplied `_d0_executor`/`_al_executor`; no Linux-native executor is selected. | Keep the guards. For Phase B, introduce a separately reviewed 0259/0263-only one-TRAN Canary route; do not connect ordinary D0/AL or expand the allowlist. |
| Windows/Linux environment | `sc_sample_hold_canary._native_environment_verified` requires `pywin32`, `win32job`, Windows DLLs and exact Windows interpreter paths; `run_guarded` rejects `os.name != "nt"` and requires Job Object evidence. `sc_0259_canary` has a POSIX process-group launcher, but its frozen output root and native-library defaults are Windows paths. | Freeze a new Ubuntu-native closure and dependency manifest: Python/PySpice/ngspice shared library/spinit hashes, exact interpreter, Linux process-group termination/reaping evidence. Do not reuse a Windows approval or closure. |
| Historical strict SHA tests | The 0263 SC-4C.3 fixture pins `run/single_stage_amplifier/run_ideal_dataset_simulations.py` to `f8b504c2…`; Batch046 has `46b7fca3…`. Their LF-normalized text is identical, but the raw-byte hashes differ. `load_plan` fails in `_asset`; all 17 `test_sc_sample_hold_generic_sc4c3.py` cases fail during class setup, before test assertions. The 0259 SC-4B.3C closure has 11 raw-byte mismatches (all LF/CRLF-only against the old Windows worktree) plus a missing archived runner. | These are old source-closure setup failures, **not** observed SampleHold functional failures. Preserve old golden hashes and tests as Windows historical evidence. Re-review and freeze a new Linux closure using actual Ubuntu raw bytes; do not normalize bytes to pass the closure. |
| Windows-specific interface tests | `test_sample_hold_al_pilot_readiness.py` imports `_validate_sample_hold_pilot`, an entrypoint absent from Batch046 `ActiveLearning/main.py`; collection fails. | Retain as historical Windows-interface validation. Replace for Ubuntu with tests of `validate_pilot_static`, `--config-only`, injected fake executor, and fail-closed physical gates; do not recreate the old function solely to satisfy the test. |
| Deliberate safety switches | Candidate JSON remains `candidate_unapproved`, `real_execution_authorized=false`, `operator_fixture_approved=false`; Phase A `main.py` refuses physical D0/AL. `sc_sample_hold_generic.load_candidate` and `sc_0259_canary.load_plan` retain strict closure checks and one-attempt contracts. | These are correct protections, not migration defects. Only a new, circuit-specific, external operator authorization and reviewed Linux closure may enable a future Canary. |

The direct source comparison found that the Windows-approved runner and Batch046 runner have identical LF-normalized text; the mismatch is solely line endings. This establishes no physical approval. The 0259 closure's other 11 present mismatches are likewise text-equal after LF normalization; `archived_runner` is absent in this worktree. The 0263 fixture's other nine source assets match their stored raw hashes.

The 17 old SC-4C.3 tests were rerun and again produced **17 setup errors, 0 executed assertions**, all at `load_plan` → `load_candidate` → `_asset`. Their waveform, topology, stimulus and fake-ledger assertions remain useful after a newly reviewed fixture/closure baseline; the `pywin32` environment check and Job Object evidence expectations are Windows-specific and require Linux process-group equivalents. The historical fixture-tamper test also writes temporary candidates under the repository root and must be adapted to an isolated Linux test directory. The old AL Pilot suite was rerun with `--collect-only` and produced **one import/collection error, 0 collected tests**: it imports the former Windows `_validate_sample_hold_pilot` from `main.py`; the Batch046 static entry is `sample_hold_phase_a.validate_pilot_static`. Collection compatibility cannot be interpreted as evaluator failure.

## Runtime chain and preserved boundaries

`onboarding/sample_hold.py:build_sample_hold_circuit_config` verifies topology, converted source, ideal archive and Stage A/B; 0259 binds three runtime variables, 0263 six. `SampleHoldCandidateBuilder.expand_candidate` validates range and scales physical constructor arguments. `runtime_provider._sample_hold_evaluator_factory` constructs `SampleHoldEvaluatorAdapter`; absent an injected executor, `evaluate` fails closed. `SampleHoldFunctionalValidator.validate` keeps parameter, simulation, measurement, functional and target statuses separate. No invalid functional result can become `meets_spec=True`.

`sample_hold_phase_a.validate_pilot_static` admits only 0259/0263 reviewed Pilot paths, exact three frozen targets, `d0_mode=reuse`, `initialize_if_missing=false`, 20 unique D0 records and expected raw D0 SHA. The normal AL path checks the D0 fingerprint before its Phase A physical refusal. A missing reused D0 is rejected before native execution. `sample_hold_batch.py` contains historical Windows D0/AL executor and attempt-ledger contracts, but it is not selected by Phase A. No mock-only success is evidence of Ubuntu PySpice/ngspice operation.

The old 0263 native child uses PySpice/ngspice; its parent requires Windows Job Object evidence. The old 0259 launcher can form a POSIX process group, but the frozen fixture/approval/output-root and environment are Windows-specific. Batch Runner V2 has POSIX process supervision, but no reviewed SampleHold Canary binding to it. Do not invoke either old physical CLI on Ubuntu.

## Phase B Canary admission (0259 first, then 0263)

1. Select a new isolated, clean Ubuntu checkout and exact Git SHA. Verify the 0259/0263 assets, converted constructor bindings, fixture stimuli and D0 hashes without changing frozen historical assets.
2. Create a **new** Linux-specific raw-byte source closure covering the exact candidate, fixture, native child, evaluator, ngspice configuration, process supervisor and dependencies. Validate Git/worktree byte identity; do not recycle the Windows closure or normalize CRLF/LF for acceptance.
3. Verify Linux Python, PySpice, NumPy, PyYAML, ngspice shared library, `spinit` and any compatibility patch with versions and file hashes. Mock-test POSIX process-group launch, timeout, descendant termination/reaping uncertainty, durable pre-attempt ledger, unique output directory and one-attempt/no-retry enforcement.
4. Keep ordinary `main.py` D0/AL guards. Use a separate 0259/0263-only Canary entrypoint whose dry-run is read-only and whose real mode requires an external one-time approval bound to the new closure, exact circuit/fixture, output root and **one native TRAN, zero retries**. Confirm absence of old claim/output before approval. No circuit_0605 admission.
5. Execute no physical attempt in Phase A.1. Phase B may request separately authorized 0259 Canary (maximum one TRAN); review result before separately approving 0263 (maximum one additional TRAN). Do not chain them automatically.

## Safe current diagnostics and offline results

From `D:\analogCkt\AG_simulation_multi_type_v2_dev` with the installed Python:

```powershell
& 'C:\Users\clair\anaconda3\python.exe' -m ActiveLearning.main --circuit-type SampleHold --circuit-id circuit_0259 --config ActiveLearning/experiments/SampleHold/circuit_0259/one_iteration_pilot_v1.yaml --config-only
& 'C:\Users\clair\anaconda3\python.exe' -m ActiveLearning.main --circuit-type SampleHold --circuit-id circuit_0263 --config ActiveLearning/experiments/SampleHold/circuit_0263/one_iteration_pilot_v1.yaml --config-only
& 'C:\Users\clair\anaconda3\python.exe' -m pytest -q -p no:cacheprovider tests/test_sample_hold_ubuntu_phase_a1.py tests/test_sample_hold_ubuntu_phase_a.py tests/test_sample_hold_al_compact_v1.py
& 'C:\Users\clair\anaconda3\python.exe' -m pytest -q -p no:cacheprovider tests/test_sample_hold_offline.py
& 'C:\Users\clair\anaconda3\python.exe' -m pytest -q -p no:cacheprovider tests/test_sample_hold_ubuntu_phase_a1.py tests/test_sample_hold_ubuntu_phase_a.py tests/test_sample_hold_al_compact_v1.py tests/test_sample_hold_offline.py
```

Observed: the new fail-closed/Phase A/compact tests **22/22 PASS** (including three new tests proving the two old fixtures reject `--execute-real-spice` before `run_guarded`); standalone evaluator tests **18/18 PASS**. A first combined invocation exposed the known order-sensitive `test_no_simulator_import` assertion (39 PASS, 1 FAIL) because another test had already imported PySpice into the same pytest process. The assertion now checks this module in a fresh interpreter, without any native simulation; the final combined run is **40/40 PASS**. Both `--config-only` commands PASS (0259=3D, 0263=6D, `d0_mode=reuse`). `git diff --check` PASS for the existing tracked Phase A diff. The earlier Phase A report recorded an additional 100 combined mock/non-regression passes; they were not rerun here. The 17 old setup errors and one old test collection error remain version/interface mismatches, not counted as new functional failures. No Linux native dependency check or physical attempt was performed on this Windows host.

`SAMPLEHOLD_UBUNTU_PHASE_A1_READINESS_REPORT.md` is ignored by the baseline `.gitignore` (`/*`); it exists in this worktree but requires selective inclusion in any later release review. No Git staging or commit was done.

## Decision

**BLOCKED for Phase B physical Canary.** The next necessary work is a reviewed Ubuntu-native closure and one-shot process/authorization route, followed by focused mock tests in the actual Ubuntu environment. The static SampleHold AL mapping is usable for inspection, but production D0/AL and physical Canary are not authorized or validated.
