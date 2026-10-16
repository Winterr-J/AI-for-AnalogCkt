from __future__ import annotations

import math
import unittest
from unittest.mock import patch

from ActiveLearning.InitializeD0.evaluator import evaluate_current_mirror
from ActiveLearning.InitializeD0.evaluator.current_mirror_evaluator import (
    CurrentMeasurement,
)
from ActiveLearning.InitializeD0.examples.CurrentMirror.circuit_0469.evaluator_adapter import (
    CIRCUIT_0469_ADAPTER,
)


class _FakeAdapter:
    name = "fake_current_mirror"
    op_reference_current = CurrentMeasurement("internal_parameters", "iref")
    op_output_current = CurrentMeasurement("internal_parameters", "iout")
    dc_sweep_source = "VOUT"
    dc_output_current = CurrentMeasurement("branches", "vout")
    mirror_ratio = 1.0
    vout_nominal = 1.0
    temperature = 25.0

    def __init__(self, circuit):
        self.circuit = circuit

    def build_circuit(self, params, vout_nominal):
        return self.circuit


class _FailingSimulator:
    def save_internal_parameters(self, *_names):
        pass

    def operating_point(self):
        raise RuntimeError("synthetic OP failure")


class _ZeroReferenceAnalysis:
    internal_parameters = {"iref": [0.0], "iout": [1.0e-4]}


class _ZeroReferenceSimulator:
    def save_internal_parameters(self, *_names):
        pass

    def operating_point(self):
        return _ZeroReferenceAnalysis()


class _FakeCircuit:
    def __init__(self, simulator):
        self._simulator = simulator

    def simulator(self, **_kwargs):
        return self._simulator


class CurrentMirrorEvaluatorRegressionTests(unittest.TestCase):
    def test_circuit_0469_adapter_preserves_signed_source_and_corrects_terminals(self) -> None:
        circuit = CIRCUIT_0469_ADAPTER.build_circuit(
            {"IB1": -1.0e-4, "R0": 10_000.0, "R1": 10_000.0},
            CIRCUIT_0469_ADAPTER.vout_nominal,
        )

        source = circuit["IIB1"]
        self.assertEqual(source.node_names, ["IB1", "0"])
        self.assertEqual(float(source.dc_value), -1.0e-4)

    def test_circuit_0469_nominal_regression(self) -> None:
        current_error, output_resistance, simulation_valid = evaluate_current_mirror(
            {"IB1": -1.0e-4, "R0": 10_000.0, "R1": 10_000.0},
            adapter=CIRCUIT_0469_ADAPTER,
        )

        self.assertIs(simulation_valid, True)
        self.assertIs(type(current_error), float)
        self.assertIs(type(output_resistance), float)
        self.assertTrue(math.isfinite(current_error))
        self.assertTrue(math.isfinite(output_resistance))
        self.assertTrue(
            math.isclose(
                current_error,
                0.019603693816695945,
                rel_tol=1.0e-12,
                abs_tol=1.0e-15,
            )
        )
        self.assertTrue(
            math.isclose(
                output_resistance,
                22974501.595001142,
                rel_tol=1.0e-10,
                abs_tol=1.0e-6,
            )
        )

    @patch(
        "ActiveLearning.InitializeD0.evaluator.current_mirror_evaluator.configure_ngspice"
    )
    def test_simulation_failure_returns_invalid_status(self, _configure_ngspice) -> None:
        adapter = _FakeAdapter(_FakeCircuit(_FailingSimulator()))

        self.assertEqual(
            evaluate_current_mirror({"X": 1.0}, adapter=adapter),
            (None, None, False),
        )

    @patch(
        "ActiveLearning.InitializeD0.evaluator.current_mirror_evaluator.configure_ngspice"
    )
    def test_extraction_failure_returns_invalid_status(self, _configure_ngspice) -> None:
        adapter = _FakeAdapter(_FakeCircuit(_ZeroReferenceSimulator()))

        self.assertEqual(
            evaluate_current_mirror({"X": 1.0}, adapter=adapter),
            (None, None, False),
        )

    def test_invalid_control_arguments_still_raise(self) -> None:
        with self.assertRaises(ValueError):
            evaluate_current_mirror(
                {"IB1": -1.0e-4, "R0": 10_000.0, "R1": 10_000.0},
                adapter=CIRCUIT_0469_ADAPTER,
                rout_half_width=0.0,
            )
        with self.assertRaises(TypeError):
            evaluate_current_mirror(
                {"IB1": -1.0e-4, "R0": 10_000.0, "R1": 10_000.0},
                adapter=CIRCUIT_0469_ADAPTER,
                rout_points="11",
            )

    def test_nonconforming_adapter_still_raises(self) -> None:
        with self.assertRaises(TypeError):
            evaluate_current_mirror({"X": 1.0}, adapter=object())


if __name__ == "__main__":
    unittest.main()
