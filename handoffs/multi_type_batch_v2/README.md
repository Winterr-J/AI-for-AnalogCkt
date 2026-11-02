# Multi-Type Batch Candidate Discovery V2 Handoff

## Purpose

This package transfers the completed Windows Multi-Type Batch Candidate Discovery V2 static audit to Ubuntu for independent, read-only production-coverage reconciliation and Gateway compatibility review.

Source audit version: `multi_type_batch_candidate_discovery_v2`, dated 2026-11-02.

## Package summary

- Archive: `multi_type_batch_v2_handoff.tar.gz`
- SHA-256: `b16d27f91d4f48c5b758bed30cdcc4ffb85b91b0fe5280520d90e99afc9290fb`
- Provisional manifests: 28
- Unique circuit IDs across manifests: 254
- Archive entries: 34

The archive preserves repository-relative paths below:

- `reports/multi_type_batch_candidate_discovery_v2/screening_summary.md`
- `reports/multi_type_batch_candidate_discovery_v2/candidate_inventory.csv`
- `reports/multi_type_batch_candidate_discovery_v2/type_batch_summary.csv`
- `reports/multi_type_batch_candidate_discovery_v2/proposed_batches.md`
- `reports/multi_type_batch_candidate_discovery_v2/v1_vs_v2_comparison.md`
- `reports/multi_type_batch_candidate_discovery_v2/UBUNTU_HANDOFF_AUDIT.md`
- `reports/multi_type_batch_candidate_discovery_v2/batch_manifests/MTV2_CMP_001.json`
- `reports/multi_type_batch_candidate_discovery_v2/batch_manifests/MTV2_CMP_002.json`
- `reports/multi_type_batch_candidate_discovery_v2/batch_manifests/MTV2_CMP_003.json`
- `reports/multi_type_batch_candidate_discovery_v2/batch_manifests/MTV2_CMP_004.json`
- `reports/multi_type_batch_candidate_discovery_v2/batch_manifests/MTV2_FIL_001.json`
- `reports/multi_type_batch_candidate_discovery_v2/batch_manifests/MTV2_LDO_001.json`
- `reports/multi_type_batch_candidate_discovery_v2/batch_manifests/MTV2_LDO_002.json`
- `reports/multi_type_batch_candidate_discovery_v2/batch_manifests/MTV2_LDO_003.json`
- `reports/multi_type_batch_candidate_discovery_v2/batch_manifests/MTV2_LDO_004.json`
- `reports/multi_type_batch_candidate_discovery_v2/batch_manifests/MTV2_OSC_001.json`
- `reports/multi_type_batch_candidate_discovery_v2/batch_manifests/MTV2_OSC_002.json`
- `reports/multi_type_batch_candidate_discovery_v2/batch_manifests/MTV2_OSC_003.json`
- `reports/multi_type_batch_candidate_discovery_v2/batch_manifests/MTV2_OSC_004.json`
- `reports/multi_type_batch_candidate_discovery_v2/batch_manifests/MTV2_OSC_005.json`
- `reports/multi_type_batch_candidate_discovery_v2/batch_manifests/MTV2_OSC_006.json`
- `reports/multi_type_batch_candidate_discovery_v2/batch_manifests/MTV2_SCA_001.json`
- `reports/multi_type_batch_candidate_discovery_v2/batch_manifests/MTV2_SCA_002.json`
- `reports/multi_type_batch_candidate_discovery_v2/batch_manifests/MTV2_SCF_001.json`
- `reports/multi_type_batch_candidate_discovery_v2/batch_manifests/MTV2_SCI_001.json`
- `reports/multi_type_batch_candidate_discovery_v2/batch_manifests/MTV2_SCI_002.json`
- `reports/multi_type_batch_candidate_discovery_v2/batch_manifests/MTV2_SH_001.json`
- `reports/multi_type_batch_candidate_discovery_v2/batch_manifests/MTV2_SH_002.json`
- `reports/multi_type_batch_candidate_discovery_v2/batch_manifests/MTV2_VREF_001.json`
- `reports/multi_type_batch_candidate_discovery_v2/batch_manifests/MTV2_VREF_002.json`
- `reports/multi_type_batch_candidate_discovery_v2/batch_manifests/MTV2_VREF_003.json`
- `reports/multi_type_batch_candidate_discovery_v2/batch_manifests/MTV2_VREF_004.json`
- `reports/multi_type_batch_candidate_discovery_v2/batch_manifests/MTV2_VREF_005.json`
- `reports/multi_type_batch_candidate_discovery_v2/batch_manifests/MTV2_VREF_006.json`

## Ubuntu verification and extraction

From the repository root after fetching this handoff branch:

```bash
cd handoffs/multi_type_batch_v2
sha256sum -c multi_type_batch_v2_handoff.tar.gz.sha256
mkdir -p /tmp/multi_type_batch_v2_handoff
tar -xzf multi_type_batch_v2_handoff.tar.gz -C /tmp/multi_type_batch_v2_handoff
```

The extracted audit starts at:

`/tmp/multi_type_batch_v2_handoff/reports/multi_type_batch_candidate_discovery_v2/`

## Important warning

All 28 manifests are provisional static-screening proposals. They are not verified Gateway manifests, are not approved for Gateway submission, and do not authorize Profiling, D0, Active Learning, SPICE, LLM, or production Batch execution. Ubuntu must first reconcile existing Batch coverage and validate the real Gateway schema and downstream preparation requirements.
