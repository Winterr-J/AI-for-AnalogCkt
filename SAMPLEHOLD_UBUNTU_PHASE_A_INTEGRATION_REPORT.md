# SampleHold Multi-Type V2 — Phase A integration report

## Gate

`SAMPLEHOLD_UBUNTU_PHASE_A_BLOCKED`

The static SampleHold mapping, prompt and frozen-D0 compatibility path is integrated and passes focused offline checks. The *physical* D0/AL executor is deliberately not enabled: the Windows-approved native source closure does not match the Ubuntu Batch046 shared runner. The Windows-specific Job Object/child execution and approval paths are not Ubuntu execution authority. No SPICE, D0, AL or external LLM was started.

## Baseline and migration strategy

- Target: `multi-type-v2/samplehold-phase1` at `a69a6af3ebf76d28d3ece8a5753535739c2d763d` (Batch046); no commit was made.
- The old `D:\analogCkt\AG_simulation` worktree was read-only. The handoff payload's 282 recorded SHA-256 identities were checked before transfer; 76 absent payload files were copied with source/destination hash equality. Two fixture-resolution JSON dependencies omitted from the payload were taken from the old worktree only after verifying their recorded hashes.
- No Ubuntu Batch Runner, Acquisition, generic expansion, or native runner file was overwritten. Shared AL modifications are limited to SampleHold registration, a compact-prompt dispatch, and optional iteration/prompt settings with legacy defaults.
- The migrated Windows physical files remain reference implementations, **not** authorization to execute on Ubuntu.
- The copied D0 example directory is ignored by the baseline `.gitignore`; it is present and hash-checked in this worktree but would **not** enter a normal future commit. The report itself is also ignored. Phase B source closure must explicitly address distribution/persistence of these assets rather than assuming Git will include them.

## Added and modified files

The principal new files are `ActiveLearning/onboarding/sample_hold.py`, `sample_hold_batch.py`, `sample_hold_batch_child.py`, `d0_execution_windows.py`, `sample_hold_phase_a.py`, `ActiveLearning/sample_hold_offline.py`, `sc_sample_hold_generic.py`, `ActiveLearning/performance_prediction/sample_hold_compact.py`, SampleHold prompt/template assets, fixture and D0 assets under `ActiveLearning/circuit_types/SampleHold`, `ActiveLearning/docs/reference_audit`, `ActiveLearning/InitializeD0/examples/SampleHold`, and the 0259/0263 pilot YAMLs under `ActiveLearning/experiments/SampleHold`. Added offline tests are `tests/test_sample_hold_offline.py`, `test_sample_hold_al_compact_v1.py`, and `test_sample_hold_ubuntu_phase_a.py`; the copied Windows physical-readiness suites are retained as migration evidence.

Modified shared files: `ActiveLearning/main.py`, `active_learning.py`, `onboarding/auto_config.py`, `onboarding/models.py`, `onboarding/runtime_provider.py`, `inverse_design/inverse_designer.py`, `inverse_design/prompt_builder.py`, and `performance_prediction/performance_predictor.py`.

## Interface and guard status

- 0259 maps exactly 3 runtime parameters; 0263 maps exactly 6. 0605's 6D mapping can be inspected statically, but the pilot guard rejects it.
- `SampleHoldALExecutor` does not declare `__call__` itself; it inherits `SampleHoldBatchExecutor.__call__`. `SampleHoldEvaluatorAdapter.evaluate` expands the parameter vector then invokes an **explicitly injected** callable executor. Without that injection it fails closed. The functional validator keeps parameter, simulation, measurement, functional and numeric-target states separate; functional invalidity cannot become `meets_spec=True`.
- Both existing 0259/0263 one- and five-iteration YAMLs pass static validation: exactly 20 unique frozen D0 records, unchanged expected JSONL SHA-256, explicit finite `<=` targets matching the frozen values, `compact_v1`, `stop_on_meets_spec=false`, one physical attempt per iteration, and zero automatic retries. Config-only inspection passed for 0259/one-iteration and 0263/five-iteration.
- The current D0 entry rejects a missing SampleHold D0 before native execution; the current AL entry validates the frozen inputs and then rejects physical execution before output creation, LLM or SPICE. This is an intentional Phase A safety boundary, **not** a completed runnable Ubuntu D0/AL executor integration. Existing D0 is read-only and no pilot results were overwritten.

## Offline results

- Combined mock/non-regression selection: **100 passed** (`test_inverse_designer`, `test_active_learning`, `test_current_mirror_al_functional`, Batch V2 recovery/time recovery, SampleHold compact prompt and new Phase A contract tests).
- Separately: `test_sample_hold_offline.py`: **18 passed**. It is run separately because its no-PySpice-import assertion checks a fresh interpreter and is order-sensitive when combined with other suites.
- The copied Windows `test_sc_sample_hold_generic_sc4c3.py` produces **17 setup errors** on this baseline because the strict native loader detects the recorded runner-hash mismatch. The copied `test_sample_hold_al_pilot_readiness.py` cannot collect because the Windows-only `_validate_sample_hold_pilot` interface is not present in Batch046. These are unresolved Phase B integration gaps, not passing tests. No nominal-regression or other known real-SPICE test was run.

## Physical blocker and Phase B recommendation

The frozen fixture expects SHA-256 `f8b504c2bfd33f3e1a3f7622f8cafe36e63d9292a140ee8bc4f3051c7249115d` for `run/single_stage_amplifier/run_ideal_dataset_simulations.py`; the Batch046 target file is `46b7fca3ccd0e34c4d1b7bff9e2a81e5bd59f1d142967ccd932a2455000dbf86`. The original strict `sc_sample_hold_generic.load_candidate` was left unchanged. The static checker reports the mismatch without treating it as physical approval. Windows Pilot output/approval locations and Windows Job Object execution also require an Ubuntu-specific review.

For Phase B, first freeze a Linux-native source/worker closure and provenance against the actual Batch046 runner, then add a thin D0/AL executor binding with the existing attempt ledger, timeout and approval semantics. Verify no D0 regeneration and isolated AL output paths; run one separately authorized, bounded 0259 physical Canary, then 0263. Do not use 0605 for formal AL until its circuit mapping/fixture and physical closure have been individually approved.

No Git commit or push was performed.
