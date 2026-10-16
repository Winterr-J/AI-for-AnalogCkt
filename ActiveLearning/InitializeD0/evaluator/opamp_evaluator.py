"""Production OpAmp V1 Mode-2 formal return-ratio evaluator."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

import numpy as np

from ActiveLearning.onboarding.generic_adapter import GenericPySpiceBuilder
from ActiveLearning.onboarding.generic_testbench import (
    FeedbackEdgeRequest,
    GenericTestbenchDecorator,
    LoadAttachment,
    NativeOperatingPoint,
    SourceSpecification,
    derive_feedback_dc_offset,
    native_op_nodeset,
    probe_node_voltage,
    run_decorated_operating_point,
    run_native_operating_point,
)
from ActiveLearning.onboarding.models import OpAmpSemanticMapping
from ActiveLearning.onboarding.opamp_semantics import (
    resolve_differential_input_polarity,
)
from run.single_stage_amplifier.run_ideal_dataset_simulations import configure_ngspice
from simulation_metrics import LoopGainResult, analyze_loop_gain


__all__ = [
    "OpAmpEvaluationResult",
    "OpAmpEvaluator",
    "OpAmpMetrics",
    "evaluate_opamp_mode2",
]


_SUPPORTED_MODES = {
    "existing_feedback_return_ratio",
    "synthesized_unity_feedback_return_ratio",
}


@dataclass(frozen=True)
class OpAmpMetrics:
    low_frequency_open_loop_gain_v_per_v: float | None = None
    low_frequency_open_loop_gain_db: float | None = None
    unity_gain_frequency_hz: float | None = None
    phase_margin_deg: float | None = None


@dataclass(frozen=True)
class OpAmpEvaluationResult:
    metrics: OpAmpMetrics
    diagnostics: Mapping[str, Any]
    simulation_valid: bool
    measurement_status: str
    failure_reasons: tuple[str, ...] = ()
    testbench_metadata: Mapping[str, Any] = field(default_factory=dict)
    traces: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self, *, include_traces: bool = False) -> dict[str, Any]:
        result = {
            "metrics": asdict(self.metrics),
            "diagnostics": dict(self.diagnostics),
            "simulation_valid": self.simulation_valid,
            "measurement_status": self.measurement_status,
            "failure_reasons": list(self.failure_reasons),
            "testbench_metadata": dict(self.testbench_metadata),
        }
        if include_traces:
            trace_payload: dict[str, Any] = {}
            for name, values in self.traces.items():
                array = np.asarray(values)
                if np.iscomplexobj(array):
                    trace_payload[name] = {
                        "real": array.real.tolist(),
                        "imag": array.imag.tolist(),
                    }
                else:
                    trace_payload[name] = array.tolist()
            result["traces"] = trace_payload
        return result


class OpAmpEvaluator:
    """Orchestrate one formal Mode-2 evaluation using injected runtime config."""

    def __init__(
        self,
        *,
        name: str,
        candidate_builder: GenericPySpiceBuilder,
        semantic_mapping: OpAmpSemanticMapping,
        temperature_c: float = 25.0,
        ac_start_hz: float = 1.0,
        ac_stop_hz: float = 1.0e9,
        points_per_decade: int = 40,
        low_frequency_point_count: int = 10,
        plateau_tolerance_db: float = 0.5,
        dc_preservation_tolerance_v: float = 2.0e-5,
    ) -> None:
        self.name = str(name)
        self.candidate_builder = candidate_builder
        self.semantic_mapping = semantic_mapping
        self.temperature_c = float(temperature_c)
        self.ac_start_hz = float(ac_start_hz)
        self.ac_stop_hz = float(ac_stop_hz)
        self.points_per_decade = int(points_per_decade)
        self.low_frequency_point_count = int(low_frequency_point_count)
        self.plateau_tolerance_db = float(plateau_tolerance_db)
        self.dc_preservation_tolerance_v = float(dc_preservation_tolerance_v)
        if not (0.0 < self.ac_start_hz < self.ac_stop_hz):
            raise ValueError("AC sweep must satisfy 0 < start < stop")
        if self.points_per_decade <= 0 or self.low_frequency_point_count <= 0:
            raise ValueError("AC point counts must be positive")
        if self.dc_preservation_tolerance_v <= 0.0:
            raise ValueError("dc_preservation_tolerance_v must be positive")

    def _base_diagnostics(self, mode: str | None) -> dict[str, Any]:
        return {
            "native_op_valid": False,
            "decorated_op_valid": False,
            "dc_preservation_valid": False,
            "ac_simulation_valid": False,
            "max_dc_voltage_difference": None,
            "polarity_status": self.semantic_mapping.inputs.polarity_status,
            "positive_input": self.semantic_mapping.inputs.positive.node,
            "negative_input": self.semantic_mapping.inputs.negative.node,
            "polarity_evidence": [],
            "unity_feedback_confirmed": False,
            "return_ratio_valid": False,
            "unity_crossing_count": 0,
            "selected_crossing_hz": None,
            "selected_crossing_index": None,
            "crossing_direction": None,
            "all_unity_crossings": [],
            "measurement_mode": mode,
            "load_capacitance_f": self.semantic_mapping.load_definition.capacitance_f,
            "ac_start_hz": self.ac_start_hz,
            "ac_stop_hz": self.ac_stop_hz,
            "points_per_decade": self.points_per_decade,
            "feedback_dc_offset_v": None,
            "dc_preservation_tolerance_v": self.dc_preservation_tolerance_v,
        }

    @staticmethod
    def _failure(
        status: str,
        diagnostics: Mapping[str, Any],
        *,
        reasons: tuple[str, ...] | None = None,
        simulation_valid: bool = False,
        testbench_metadata: Mapping[str, Any] | None = None,
    ) -> OpAmpEvaluationResult:
        return OpAmpEvaluationResult(
            metrics=OpAmpMetrics(),
            diagnostics=dict(diagnostics),
            simulation_valid=simulation_valid,
            measurement_status=status,
            failure_reasons=reasons or (status,),
            testbench_metadata=testbench_metadata or {},
        )

    @staticmethod
    def _source_names(circuit: Any) -> tuple[str, ...]:
        return tuple(
            str(name)
            for name in circuit.element_names
            if str(name)[:1].casefold() in {"v", "i"}
        )

    def _build_base(
        self,
        params: Mapping[str, float],
        *,
        mode: str,
    ) -> tuple[Any, GenericTestbenchDecorator]:
        circuit = self.candidate_builder.build_circuit(params)
        decorator = GenericTestbenchDecorator(circuit, mode=mode)
        source_overrides = {
            source: SourceSpecification(ac_magnitude=0.0, ac_phase_deg=0.0)
            for source in self._source_names(circuit)
        }
        decorator.apply_source_overrides(source_overrides)
        load = self.semantic_mapping.load_definition
        if load.capacitance_f is not None or load.resistance_ohm is not None:
            output_node = self.semantic_mapping.outputs.positive_node
            if output_node is None:
                raise ValueError("single-ended Mode-2 output node is unavailable")
            decorator.attach_load(
                LoadAttachment(
                    output_node=output_node,
                    reference_node="0",
                    capacitance_f=load.capacitance_f,
                    resistance_ohm=load.resistance_ohm,
                    differential=load.differential,
                    name="LOAD",
                    provenance=load.provenance,
                )
            )
        return circuit, decorator

    def _resolve_polarity(
        self,
        params: Mapping[str, float],
    ) -> dict[str, Any]:
        inputs = self.semantic_mapping.inputs
        if inputs.polarity_status == "resolved":
            return {
                "status": "resolved",
                "positive": inputs.positive.node,
                "negative": inputs.negative.node,
                "evidence": list(inputs.positive.evidence + inputs.negative.evidence),
                "low_frequency_transfer": None,
            }
        candidates = [candidate for candidate in inputs.candidates if candidate.node]
        if len(candidates) != 2:
            return {
                "status": "unavailable",
                "positive": None,
                "negative": None,
                "evidence": ["exactly two external input candidates are required"],
                "low_frequency_transfer": None,
            }
        first, second = candidates
        if first.source is None or second.source is None:
            return {
                "status": "unavailable",
                "positive": None,
                "negative": None,
                "evidence": ["input candidate source mapping is unavailable"],
                "low_frequency_transfer": None,
            }
        try:
            circuit, decorator = self._build_base(params, mode="polarity_sign_probe")
            decorator.apply_source_overrides(
                {
                    first.source: SourceSpecification(ac_magnitude=0.5, ac_phase_deg=0.0),
                    second.source: SourceSpecification(ac_magnitude=0.5, ac_phase_deg=180.0),
                }
            )
            output_node = self.semantic_mapping.outputs.positive_node
            if output_node is None:
                raise ValueError("polarity probe output is unavailable")
            analysis = circuit.simulator(
                temperature=self.temperature_c,
                nominal_temperature=self.temperature_c,
            ).ac(
                start_frequency=1.0,
                stop_frequency=10.0,
                number_of_points=10,
                variation="dec",
            )
            output = np.asarray(
                probe_node_voltage(analysis, output_node), dtype=complex
            ).reshape(-1)
            first_vector = np.asarray(
                probe_node_voltage(analysis, first.node), dtype=complex
            ).reshape(-1)
            second_vector = np.asarray(
                probe_node_voltage(analysis, second.node), dtype=complex
            ).reshape(-1)
            differential = first_vector - second_vector
            valid = np.abs(differential) > 1.0e-24
            if not np.all(valid) or len(output) != len(differential):
                raise ValueError("polarity probe differential excitation is invalid")
            transfer = output / differential
            low = complex(np.median(transfer[: min(10, len(transfer))]))
            resolved = resolve_differential_input_polarity(
                first.node,
                second.node,
                low,
            )
            return {
                "status": resolved["status"],
                "positive": resolved["positive"],
                "negative": resolved["negative"],
                "evidence": [
                    "1-10 Hz differential AC sign probe; no performance threshold",
                    f"low_frequency_transfer={low!r}",
                ],
                "low_frequency_transfer": low,
                "reason": resolved["reason"],
            }
        except Exception as error:
            return {
                "status": "unavailable",
                "positive": None,
                "negative": None,
                "evidence": [f"polarity sign probe failed: {type(error).__name__}: {error}"],
                "low_frequency_transfer": None,
                "reason": "polarity_probe_failed",
            }

    @staticmethod
    def _source_for_node(mapping: OpAmpSemanticMapping, node: str) -> str | None:
        target = node.casefold()
        for candidate in mapping.inputs.candidates:
            if candidate.node and candidate.node.casefold() == target:
                return candidate.source
        return None

    def _decorate(
        self,
        params: Mapping[str, float],
        mode: str,
        native_op: NativeOperatingPoint,
        polarity: Mapping[str, Any],
    ) -> tuple[Any, GenericTestbenchDecorator, str, float]:
        circuit, decorator = self._build_base(params, mode=mode)
        output_node = self.semantic_mapping.outputs.positive_node
        if output_node is None:
            raise ValueError("Mode-2 output node is unavailable")
        nodeset = native_op_nodeset(native_op)
        if mode == "existing_feedback_return_ratio":
            if len(self.semantic_mapping.feedback.edges) != 1:
                raise ValueError("existing feedback mode requires exactly one semantic edge")
            edge = self.semantic_mapping.feedback.edges[0]
            if edge.to_terminal is None:
                raise ValueError("existing feedback edge has no terminal binding")
            feedback_dc_offset = 0.0
            destination = decorator.decorate_feedback_edge(
                FeedbackEdgeRequest(
                    from_node=edge.from_node,
                    to_terminal=edge.to_terminal,
                    dc_value=0.0,
                    ac_magnitude=1.0,
                    ac_phase_deg=0.0,
                    feedback_factor=edge.factor,
                    unity_feedback_confirmed=(
                        self.semantic_mapping.feedback.unity_feedback_confirmed
                        and math.isclose(edge.factor, 1.0)
                    ),
                ),
                native_operating_point=native_op,
            )
            nodeset[destination] = self._node_value(native_op.node_voltages, edge.from_node)
            decorator.apply_nodeset(nodeset)
        else:
            negative_input = polarity.get("negative")
            if not isinstance(negative_input, str):
                raise ValueError("synthesized feedback negative input is unresolved")
            negative_source = self._source_for_node(
                self.semantic_mapping, negative_input
            )
            if negative_source is None:
                raise ValueError("negative input source mapping is unavailable")
            feedback_dc_offset = derive_feedback_dc_offset(
                native_op,
                output_node=output_node,
                feedback_input_node=negative_input,
            )
            destination = decorator.decorate_feedback_edge(
                FeedbackEdgeRequest(
                    from_node=output_node,
                    to_node=negative_input,
                    remove_elements=(negative_source,),
                    dc_value=feedback_dc_offset,
                    ac_magnitude=1.0,
                    ac_phase_deg=0.0,
                    feedback_factor=1.0,
                    unity_feedback_confirmed=True,
                ),
                native_operating_point=native_op,
                nodeset=nodeset,
            )
        return circuit, decorator, destination, feedback_dc_offset

    @staticmethod
    def _node_value(values: Mapping[str, float], name: str) -> float:
        target = name.casefold()
        for key, value in values.items():
            if str(key).casefold() == target:
                return float(value)
        raise KeyError(name)

    def _dc_preservation(
        self,
        native: NativeOperatingPoint,
        decorated: NativeOperatingPoint,
        *,
        feedback_node: str,
        mode: str,
    ) -> dict[str, Any]:
        decorated_folded = {
            str(name).casefold(): float(value)
            for name, value in decorated.node_voltages.items()
        }
        differences: dict[str, float] = {}
        for name, value in native.node_voltages.items():
            key = str(name).casefold()
            if key in decorated_folded:
                differences[str(name)] = abs(float(value) - decorated_folded[key])
        if mode == "existing_feedback_return_ratio":
            output_node = self.semantic_mapping.outputs.positive_node
            if output_node is not None:
                try:
                    differences[f"{feedback_node}<->{output_node}"] = abs(
                        self._node_value(decorated.node_voltages, feedback_node)
                        - self._node_value(native.node_voltages, output_node)
                    )
                except KeyError:
                    pass
        maximum = max(differences.values(), default=math.inf)
        return {
            "valid": bool(differences) and maximum <= self.dc_preservation_tolerance_v,
            "max_abs_voltage_difference": maximum,
            "node_abs_differences_v": differences,
        }

    def evaluate(self, params: Mapping[str, float]) -> OpAmpEvaluationResult:
        modes = [
            mode
            for mode in self.semantic_mapping.measurement_modes
            if mode in _SUPPORTED_MODES
        ]
        mode = modes[0] if len(modes) == 1 else None
        diagnostics = self._base_diagnostics(mode)
        if mode is None:
            return self._failure(
                "unsupported_measurement_mode",
                diagnostics,
                reasons=("unsupported_measurement_mode",),
            )
        try:
            configure_ngspice()
            native_circuit, native_decorator = self._build_base(
                params, mode="native_operating_point"
            )
        except Exception as error:
            diagnostics["native_op_error"] = f"{type(error).__name__}: {error}"
            return self._failure("native_op_failed", diagnostics)

        native_op = run_native_operating_point(
            native_circuit,
            temperature=self.temperature_c,
            nominal_temperature=self.temperature_c,
        )
        diagnostics["native_op_valid"] = native_op.simulation_valid
        diagnostics["native_node_voltages"] = dict(native_op.node_voltages)
        diagnostics["native_branch_currents"] = dict(native_op.branch_currents)
        native_metadata = native_decorator.finalize().metadata
        if not native_op.simulation_valid:
            diagnostics["native_op_error"] = native_op.error
            return self._failure(
                "native_op_failed",
                diagnostics,
                testbench_metadata={"native": native_metadata},
            )

        polarity = self._resolve_polarity(params)
        diagnostics["polarity_status"] = polarity["status"]
        diagnostics["positive_input"] = polarity.get("positive")
        diagnostics["negative_input"] = polarity.get("negative")
        diagnostics["polarity_evidence"] = polarity.get("evidence", [])
        if polarity["status"] != "resolved":
            return self._failure(
                "polarity_unresolved",
                diagnostics,
                reasons=("polarity_unresolved",),
                simulation_valid=True,
                testbench_metadata={"native": native_metadata},
            )

        try:
            decorated_circuit, decorator, feedback_node, feedback_dc_offset = (
                self._decorate(params, mode, native_op, polarity)
            )
        except Exception as error:
            diagnostics["feedback_decoration_error"] = (
                f"{type(error).__name__}: {error}"
            )
            return self._failure(
                "feedback_decoration_failed",
                diagnostics,
                simulation_valid=True,
                testbench_metadata={"native": native_metadata},
            )
        diagnostics["feedback_dc_offset_v"] = feedback_dc_offset
        decorated_metadata = decorator.finalize().metadata
        feedback_metadata = decorated_metadata.get("feedback_edge") or {}
        unity_confirmed = bool(feedback_metadata.get("unity_feedback_confirmed"))
        diagnostics["unity_feedback_confirmed"] = unity_confirmed
        testbench_metadata = {
            "native": native_metadata,
            "decorated": decorated_metadata,
        }
        if not unity_confirmed:
            return self._failure(
                "unsupported_measurement_mode",
                diagnostics,
                reasons=("unity_feedback_not_confirmed",),
                simulation_valid=True,
                testbench_metadata=testbench_metadata,
            )

        decorated_op = run_decorated_operating_point(
            decorated_circuit,
            temperature=self.temperature_c,
            nominal_temperature=self.temperature_c,
        )
        diagnostics["decorated_op_valid"] = decorated_op.simulation_valid
        if not decorated_op.simulation_valid:
            diagnostics["decorated_op_error"] = decorated_op.error
            return self._failure(
                "decorated_op_failed",
                diagnostics,
                simulation_valid=False,
                testbench_metadata=testbench_metadata,
            )
        preservation = self._dc_preservation(
            native_op,
            decorated_op,
            feedback_node=feedback_node,
            mode=mode,
        )
        diagnostics["dc_preservation_valid"] = preservation["valid"]
        diagnostics["max_dc_voltage_difference"] = preservation[
            "max_abs_voltage_difference"
        ]
        diagnostics["dc_node_abs_differences_v"] = preservation[
            "node_abs_differences_v"
        ]
        if not preservation["valid"]:
            return self._failure(
                "dc_preservation_invalid",
                diagnostics,
                simulation_valid=True,
                testbench_metadata=testbench_metadata,
            )

        try:
            analysis = decorated_circuit.simulator(
                temperature=self.temperature_c,
                nominal_temperature=self.temperature_c,
            ).ac(
                start_frequency=self.ac_start_hz,
                stop_frequency=self.ac_stop_hz,
                number_of_points=self.points_per_decade,
                variation="dec",
            )
            frequency = np.asarray(analysis.frequency, dtype=float).reshape(-1)
            output_node = self.semantic_mapping.outputs.positive_node
            assert output_node is not None
            output = np.asarray(
                probe_node_voltage(analysis, output_node), dtype=complex
            ).reshape(-1)
            injected_side = np.asarray(
                probe_node_voltage(analysis, feedback_node), dtype=complex
            ).reshape(-1)
            if len(output) != len(injected_side) or len(output) != len(frequency):
                raise ValueError("AC return-ratio vectors have mismatched lengths")
            if (
                len(frequency) < 2
                or not np.all(np.isfinite(frequency))
                or np.any(frequency <= 0.0)
                or np.any(np.diff(frequency) <= 0.0)
                or not np.all(np.isfinite(output.real))
                or not np.all(np.isfinite(output.imag))
                or not np.all(np.isfinite(injected_side.real))
                or not np.all(np.isfinite(injected_side.imag))
            ):
                raise ValueError("AC response vectors are not finite and ordered")
            valid_denominator = np.abs(injected_side) > 1.0e-24
            return_ratio = np.full(output.shape, complex(np.nan, np.nan))
            return_ratio[valid_denominator] = (
                -output[valid_denominator] / injected_side[valid_denominator]
            )
            diagnostics["ac_simulation_valid"] = True
        except Exception as error:
            diagnostics["ac_simulation_error"] = f"{type(error).__name__}: {error}"
            return self._failure(
                "ac_simulation_failed",
                diagnostics,
                simulation_valid=False,
                testbench_metadata=testbench_metadata,
            )

        try:
            loop_gain = analyze_loop_gain(
                frequency,
                return_ratio,
                low_frequency_point_count=self.low_frequency_point_count,
                plateau_tolerance_db=self.plateau_tolerance_db,
            )
        except Exception as error:
            diagnostics["return_ratio_error"] = f"{type(error).__name__}: {error}"
            return self._failure(
                "return_ratio_invalid",
                diagnostics,
                simulation_valid=True,
                testbench_metadata=testbench_metadata,
            )
        self._update_loop_diagnostics(diagnostics, loop_gain)
        traces = {
            "frequency_hz": loop_gain.frequency_hz,
            "return_ratio": loop_gain.return_ratio,
            "magnitude": loop_gain.magnitude,
            "gain_db": loop_gain.gain_db,
            "phase_deg": loop_gain.phase_deg,
        }
        if not loop_gain.return_ratio_valid:
            return OpAmpEvaluationResult(
                metrics=OpAmpMetrics(),
                diagnostics=diagnostics,
                simulation_valid=True,
                measurement_status="return_ratio_invalid",
                failure_reasons=("return_ratio_invalid",),
                testbench_metadata=testbench_metadata,
                traces=traces,
            )
        metrics = OpAmpMetrics(
            low_frequency_open_loop_gain_v_per_v=(
                loop_gain.low_frequency_gain_v_per_v
                if unity_confirmed
                else None
            ),
            low_frequency_open_loop_gain_db=(
                loop_gain.low_frequency_gain_db if unity_confirmed else None
            ),
            unity_gain_frequency_hz=(
                None
                if loop_gain.selected_crossing is None
                else loop_gain.selected_crossing.frequency_hz
            ),
            phase_margin_deg=loop_gain.phase_margin_deg,
        )
        status = "success"
        reasons = list(loop_gain.failure_reasons)
        if metrics.low_frequency_open_loop_gain_db is None:
            status = "low_frequency_gain_unavailable"
        elif metrics.unity_gain_frequency_hz is None:
            status = "no_valid_unity_crossing"
        elif metrics.phase_margin_deg is None:
            status = "phase_margin_unavailable"
        if len(loop_gain.unity_crossings) > 1:
            reasons.append("multiple_unity_crossings")
        return OpAmpEvaluationResult(
            metrics=metrics,
            diagnostics=diagnostics,
            simulation_valid=True,
            measurement_status=status,
            failure_reasons=tuple(dict.fromkeys(reasons)),
            testbench_metadata=testbench_metadata,
            traces=traces,
        )

    @staticmethod
    def _update_loop_diagnostics(
        diagnostics: dict[str, Any],
        loop_gain: LoopGainResult,
    ) -> None:
        diagnostics["return_ratio_valid"] = loop_gain.return_ratio_valid
        diagnostics["low_frequency_plateau_spread_db"] = (
            loop_gain.low_frequency_plateau_spread_db
        )
        diagnostics["unity_crossing_count"] = len(loop_gain.unity_crossings)
        diagnostics["all_unity_crossings"] = [
            asdict(crossing) for crossing in loop_gain.unity_crossings
        ]
        if loop_gain.selected_crossing is not None:
            diagnostics["selected_crossing_hz"] = (
                loop_gain.selected_crossing.frequency_hz
            )
            diagnostics["selected_crossing_index"] = (
                loop_gain.selected_crossing.index
            )
            diagnostics["crossing_direction"] = (
                loop_gain.selected_crossing.direction
            )
        diagnostics["phase_at_unity_deg"] = loop_gain.phase_at_unity_deg


def evaluate_opamp_mode2(
    params: Mapping[str, float],
    *,
    evaluator: OpAmpEvaluator,
) -> OpAmpEvaluationResult:
    """Functional entry point matching the existing evaluator style."""

    return evaluator.evaluate(params)
