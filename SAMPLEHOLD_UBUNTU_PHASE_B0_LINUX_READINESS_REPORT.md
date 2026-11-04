# SampleHold Ubuntu Phase B0 — Linux native canary readiness

## Gate

`SAMPLEHOLD_UBUNTU_PHASE_B0_CODE_READY`

This is a **code-only** gate. No Ubuntu closure has been generated or approved in this Windows session. No physical execution is authorized. The Phase A D0/AL guard remains closed; only a separately approved, one-attempt `circuit_0259` diagnostic Canary is exposed. `circuit_0263` and `circuit_0605` have no Linux physical entry point in this change.

Target branch `multi-type-v2/samplehold-phase1`, initial HEAD `bcca2161aab1c28a7f780b6515772b39c8bbcf82`. No commit or push was made.

## Changed files and execution chain

- `ActiveLearning/sample_hold_linux_canary.py` (new): Linux-only closure capture/verification, frozen 0259 fixture/stimulus check, separate approval, one-shot experiment and durable reservation/finish ledger. It checks the actual Python executable/version, PySpice/NumPy/SciPy/PyYAML/cffi versions, explicitly selected loadable `libngspice.so.0` alias, resolved shared-object bytes and hash. `PYTHONPATH` and external `SPICE_LIB_DIR` are refused. The 0259 topology, converted PySpice source, parameter archive, fixture and execution modules are all in the Ubuntu raw-byte closure. The historical Windows source closure is retained as provenance, not used as Linux execution authority.
- `ActiveLearning/sample_hold_linux_canary_child.py` (new): verifies frozen closure, exact one-attempt intent, lock, closure identity and exclusive worker claim before importing/configuring PySpice. It uses the existing 0259 fixed circuit/ideal model/stimulus, performs one TRAN, and persists raw waveform provenance.
- `ActiveLearning/sample_hold_posix.py` (new): starts the child in an independent session, applies a bounded timeout, sends SIGKILL to the **whole process group even if the leader already exited**, reaps the direct child and fails closed when a descendant/cleanup state remains uncertain. No Windows Job Object call is made on this path.
- `tests/test_sample_hold_linux_b0.py` (new): offline closure, guard, ledger, alias identity and process-group Mock tests.
- This report (new). The original Windows canary, generic canary, SampleHold executor, AL loop, D0 path and all historical data are unchanged.

The existing `SampleHoldALExecutor` inherits `SampleHoldBatchExecutor.__call__`; it does not expose a direct `evaluate` method. `SampleHoldEvaluatorAdapter.evaluate` invokes the explicitly injected callable. Its `_child_command()` still targets `sample_hold_batch_child`, and its default launcher originates from the historic `sc_0259_canary` implementation. The new Linux Canary **does not** wire that executor into D0 or AL. Phase A's Windows source-closure mismatch and formal Pilot block remain in force. This is deliberate B0 scope isolation, not a claim that Ubuntu D0/AL is runnable.

## Immutable physical contract

The Linux entry reconstructs the 0259 fixture from `SC_0259_TRANSIENT_FIXTURE_V1_SC4B3C.json` and checks a platform-neutral semantic SHA-256 `29329d5284d0e0dc6c20d1bf748e92d9776291149706e5f28b14be0c78e664e2`, its original Windows identity, the abstract fixture hash and stimulus hash. The old Windows source-closure record is preserved but does not substitute for Ubuntu raw hashes. The Linux experiment identity is `sc0259_linux_native_canary_v1`, independent of Windows and of Pilot D0/AL. Its hard bounds are one logical evaluation, one physical TRAN, zero automatic retries, 180-second attempt timeout and 240-second whole-run limit.

Before child launch, `native_attempt_reserved` is fsynced to `physical_attempt_ledger.jsonl`; an existing experiment directory or lock refuses replay. The child can claim the attempt only once. Successful and failed attempts append terminal events. Uncertain process-group cleanup retains the lock for manual audit. No D0 initialization or AL entry is called.

## Offline verification

- Focused B0, Phase A/guard, SampleHold Compact, Inverse Design, AL, CurrentMirror and Batch V2 recovery/time-recovery selection: **111 passed**.
- Fresh-interpreter SampleHold waveform/fixture suite: **18 passed** (run separately due to its PySpice-import isolation assertion).
- Mock coverage includes: draft closure rejected; source-byte/environment mismatch rejected; missing acknowledgement rejected; approval binding; reservation persisted before child invocation; failed attempt cannot replay; timeout sends group SIGKILL and reaps; leader-exited/live-descendant group is rejected; uncertain group remains fail-closed.
- No external LLM, ngspice TRAN, D0, AL or real candidate execution occurred. Linux native process isolation and actual library loading must still be checked on Ubuntu after deploying these files; Windows mocks do not certify Ubuntu runtime behavior.
- Previously documented copied Windows-only suites remain incompatible with Batch046 (`test_sc_sample_hold_generic_sc4c3.py` source-hash setup errors; `test_sample_hold_al_pilot_readiness.py` legacy Windows validation interface missing). They were not counted as passing B0 tests, and B0 does not claim to fix them.

## Ubuntu Source Closure procedure — required before any approval

Run in the **actual Ubuntu checkout containing these B0 source files and the verified 0259 resources**. Use the same intended Python interpreter for capture, verification and eventual execution. Determine the installed absolute library alias first; do not substitute a Windows hash or assume the path below exists:

```bash
cd /home/ubuntu/AG_simulation_multi_type_v2_dev
ldconfig -p | grep 'libngspice.so.0'
python3.12 -B -m ActiveLearning.sample_hold_linux_canary capture-closure \
  --ngspice-library /usr/lib/x86_64-linux-gnu/libngspice.so.0 \
  --output-root /home/ubuntu/sc0259_linux_canary_runs \
  --output /home/ubuntu/sc0259_linux_closure_draft.json
python3.12 -B -m ActiveLearning.sample_hold_linux_canary verify-closure \
  --closure /home/ubuntu/sc0259_linux_closure_draft.json
```

Replace the example checkout/library path with the **observed** Ubuntu paths. Capture creates a new `draft_unapproved` file and never overwrites one. A human must review the exact source-file set, raw-byte hashes, resolved `libngspice` hash, Python/package identities, fixture and budget. Only after review, create a separately named frozen copy with `status` changed to `frozen_verified`; do not auto-refresh hashes. Re-run `verify-closure` on that frozen file and record its SHA-256. Any missing file, changed hash, changed interpreter/package/library, or unreviewed draft fails closed.

The operator must then create a separate approval JSON **outside the checkout** with schema `sample_hold_0259_linux_operator_approval_v1`, intent `one_real_transient`, `circuit_id=circuit_0259`, `experiment_id=sc0259_linux_native_canary_v1`, the frozen closure's raw-file SHA-256 as `source_closure_sha256`, its `fixture_file_sha256`, `fixture_semantic_sha256=29329d5284d0e0dc6c20d1bf748e92d9776291149706e5f28b14be0c78e664e2`, the exact budget above, `allow_replay=false`, matching frozen `output_root`, nonempty `approved_by`, and timezone-aware ISO-8601 `approved_at`. No operator approval was created in this task.

## Future one-time 0259 physical entry (not executed)

After Ubuntu closure review and **separate explicit operator authorization only**:

```bash
cd /home/ubuntu/AG_simulation_multi_type_v2_dev
python3.12 -B -m ActiveLearning.sample_hold_linux_canary execute-real-spice \
  --closure /home/ubuntu/sc0259_linux_closure_frozen.json \
  --approval-file /home/ubuntu/sc0259_linux_operator_approval.json \
  --acknowledge-approval
```

This command is intentionally not runnable with the draft closure or without the independent approval. It is not a D0/AL launch command. Before any Ubuntu physical run, deploy and independently verify these B0 files/resources, run the Ubuntu-specific offline tests, inspect the frozen closure and approval, and confirm the designated output experiment/lock does not already exist. `circuit_0263`/`0605` remain blocked for physical execution.
