"""Old Windows Canary fixtures remain fail-closed in the Batch046 Phase A tree.

Mock-only regression: these tests must never import PySpice or start a child.
"""

from pathlib import Path
from unittest import mock

import pytest

from ActiveLearning.sc_0259_canary import CanaryError
from ActiveLearning.sc_sample_hold_generic import GenericCanaryError
from ActiveLearning.onboarding.sample_hold_phase_a import verify_static_candidate


ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "ActiveLearning/docs/reference_audit"


def test_0259_old_closure_rejects_before_guarded_launcher():
    from ActiveLearning import sc_0259_canary

    fixture = AUDIT / "SC_0259_TRANSIENT_FIXTURE_V1_SC4B3C.json"
    with mock.patch.object(sc_0259_canary, "run_guarded") as launcher:
        with pytest.raises(CanaryError, match="source closure file mismatch"):
            sc_0259_canary.main(["--fixture", str(fixture), "--execute-real-spice"])
        launcher.assert_not_called()


def test_0263_old_closure_rejects_before_guarded_launcher():
    from ActiveLearning import sc_sample_hold_canary

    candidate = AUDIT / "SC_0263_CANARY_FIXTURE_CANDIDATE_SC4C4.json"
    with mock.patch.object(sc_sample_hold_canary, "run_guarded") as launcher:
        with pytest.raises(GenericCanaryError, match="source asset path or raw hash mismatch"):
            sc_sample_hold_canary.main(["--candidate", str(candidate),
                                        "--execute-real-spice"])
        launcher.assert_not_called()


def test_0263_static_mapping_is_not_physical_source_approval():
    candidate = AUDIT / "SC_0263_CANARY_FIXTURE_CANDIDATE_SC4C4.json"
    result = verify_static_candidate(candidate)
    assert result["fixture"].mapping.circuit_id == "circuit_0263"
    assert result["native_source_closure_verified"] is False
