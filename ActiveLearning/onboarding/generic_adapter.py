"""Generic executable PySpice adapter driven by Stage A/B metadata."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ActiveLearning.InitializeD0.evaluator.current_mirror_evaluator import (
    CurrentMeasurement,
)
from ActiveLearning.InitializeD0.validation.parameter_validator import validate_parameters

from .errors import BindingValidationError, ReferenceStimulusUnavailableError
from .generic_testbench import GenericTestbenchDecorator
from .models import CurrentMirrorSemanticMapping, RuntimeParameter


class GenericPySpiceBuilder:
    """Circuit-type-neutral candidate construction and binding validation."""

    def __init__(
        self,
        *,
        name: str,
        topology_factory: Callable[..., Any],
        parameter_template: Mapping[str, Any],
        runtime_parameters: Sequence[RuntimeParameter],
        fixed_arguments: Mapping[str, Any],
        model_definitions: Sequence[Mapping[str, Any]] = (),
        normalize_ground_current_sources: Sequence[str] = (),
        raw_spice_options: str | None = None,
    ) -> None:
        self.name = name
        self._topology_factory = topology_factory
        self._parameter_template = parameter_template
        self._runtime_parameters = tuple(runtime_parameters)
        self._fixed_arguments = dict(fixed_arguments)
        self._model_definitions = tuple(dict(item) for item in model_definitions)
        self._current_source_elements = tuple(normalize_ground_current_sources)
        self._raw_spice_options = raw_spice_options

    @property
    def runtime_parameters(self) -> tuple[RuntimeParameter, ...]:
        return self._runtime_parameters

    def build_circuit(
        self,
        params: Mapping[str, float],
        *,
        argument_overrides: Mapping[str, Any] | None = None,
        stimulus_overrides: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> Any:
        expected = {item.name for item in self._runtime_parameters}
        received = set(params)
        if received != expected:
            missing = sorted(expected - received)
            unexpected = sorted(received - expected)
            details = []
            if missing:
                details.append(f"missing={missing}")
            if unexpected:
                details.append(f"unexpected={unexpected}")
            raise BindingValidationError(
                f"{self.name}: optimizer parameter set mismatch: " + ", ".join(details)
            )

        if self._runtime_parameters:
            validation = validate_parameters(params, self._parameter_template)
            if not validation["parameter_valid"]:
                raise BindingValidationError(
                    f"{self.name}: parameter validation failed: "
                    + "; ".join(validation["violations"])
                )

        kwargs = dict(self._fixed_arguments)
        kwargs.update(params)
        if argument_overrides:
            kwargs.update(argument_overrides)
        try:
            circuit = self._topology_factory(**kwargs)
        except Exception as error:
            raise BindingValidationError(
                f"{self.name}: topology build failed with configured arguments: {error}"
            ) from error

        self._normalize_ground_referenced_current_sources(circuit)
        self._install_models(circuit)
        self._validate_executable_bindings(circuit, params)
        if stimulus_overrides:
            GenericTestbenchDecorator(
                circuit,
                mode="candidate_source_override",
            ).apply_source_overrides(stimulus_overrides)
        if self._raw_spice_options:
            circuit.raw_spice += self._raw_spice_options.rstrip() + "\n"
        return circuit

    def validate_nominal_bindings(
        self,
        *,
        argument_overrides: Mapping[str, Any] | None = None,
    ) -> Any:
        nominal = {item.name: item.nominal for item in self._runtime_parameters}
        return self.build_circuit(nominal, argument_overrides=argument_overrides)

    def _normalize_ground_referenced_current_sources(self, circuit: Any) -> None:
        for element_name in self._current_source_elements:
            try:
                source = circuit[element_name]
            except Exception as error:
                raise BindingValidationError(
                    f"{self.name}: current-source binding instance {element_name!r} "
                    "does not exist"
                ) from error
            nodes = [str(name) for name in source.node_names]
            folded = [name.casefold() for name in nodes]
            if len(nodes) != 2:
                raise BindingValidationError(
                    f"{self.name}: current source {element_name!r} has unsupported "
                    f"terminals {nodes!r}"
                )
            if folded[0] == "0" and folded[1] != "0":
                value = source.dc_value
                source.detach()
                instance_name = str(element_name)[1:]
                circuit.I(instance_name, nodes[1], circuit.gnd, value)
            elif folded[1] != "0":
                raise BindingValidationError(
                    f"{self.name}: current source {element_name!r} is not "
                    f"ground-referenced: {nodes!r}"
                )

    def _install_models(self, circuit: Any) -> None:
        existing = {str(name).casefold() for name in getattr(circuit, "models", {})}
        for definition in self._model_definitions:
            name = str(definition["name"])
            if name.casefold() in existing:
                continue
            circuit.model(
                name,
                str(definition["model_type"]),
                **dict(definition["parameters"]),
            )

    def _validate_executable_bindings(
        self,
        circuit: Any,
        params: Mapping[str, float],
    ) -> None:
        for parameter in self._runtime_parameters:
            for binding in parameter.bindings:
                try:
                    element = circuit[binding.element_name]
                except Exception as error:
                    raise BindingValidationError(
                        "Cannot resolve binding for parameter "
                        f"{parameter.name}; instance={binding.element_name}; "
                        f"field={binding.field}"
                    ) from error
                if not hasattr(element, binding.pyspice_attribute):
                    raise BindingValidationError(
                        "Cannot resolve binding for parameter "
                        f"{parameter.name}; instance={binding.element_name}; "
                        f"field={binding.field}; PySpice attribute "
                        f"{binding.pyspice_attribute!r} is unavailable"
                    )
                if not binding.direct_value:
                    continue
                try:
                    actual = float(getattr(element, binding.pyspice_attribute))
                    expected = float(params[parameter.name])
                except (TypeError, ValueError) as error:
                    raise BindingValidationError(
                        f"{self.name}: binding value for {parameter.name} is not numeric"
                    ) from error
                if not math.isclose(actual, expected, rel_tol=1.0e-9, abs_tol=1.0e-15):
                    raise BindingValidationError(
                        f"{self.name}: binding mismatch for {parameter.name}; "
                        f"instance={binding.element_name}; field={binding.field}; "
                        f"expected={expected!r}; actual={actual!r}"
                    )


class GenericPySpiceAdapter:
    """Build and validate a topology without circuit-ID-specific Python code."""

    def __init__(
        self,
        *,
        circuit_id: str,
        topology_factory: Callable[..., Any],
        parameter_template: Mapping[str, Any],
        runtime_parameters: Sequence[RuntimeParameter],
        fixed_arguments: Mapping[str, Any],
        model_definitions: Sequence[Mapping[str, Any]],
        current_source_elements: Sequence[str],
        semantic_mapping: CurrentMirrorSemanticMapping,
        temperature: float = 25.0,
    ) -> None:
        self.name = circuit_id
        self._topology_factory = topology_factory
        self._parameter_template = parameter_template
        self._runtime_parameters = tuple(runtime_parameters)
        self._fixed_arguments = dict(fixed_arguments)
        self._model_definitions = tuple(dict(item) for item in model_definitions)
        self._current_source_elements = tuple(current_source_elements)
        self._semantic_mapping = semantic_mapping
        self.temperature = float(temperature)
        self.candidate_builder = GenericPySpiceBuilder(
            name=circuit_id,
            topology_factory=topology_factory,
            parameter_template=parameter_template,
            runtime_parameters=runtime_parameters,
            fixed_arguments=fixed_arguments,
            model_definitions=model_definitions,
            normalize_ground_current_sources=current_source_elements,
            raw_spice_options=(
                ".options noacct gmin=1e-12 rshunt=1e12 reltol=1e-4 "
                "abstol=1e-12 vntol=1e-6 method=gear"
            ),
        )

        reference = semantic_mapping.reference_current_measurement
        output = semantic_mapping.output_current_measurement
        if output.dc_section is None or output.dc_name is None:
            raise BindingValidationError(
                f"{circuit_id}: output current mapping has no DC measurement"
            )
        self.op_reference_current = CurrentMeasurement(
            reference.op_section,
            reference.op_name,
        )
        self.op_output_current = CurrentMeasurement(output.op_section, output.op_name)
        self.dc_sweep_source = semantic_mapping.sweep_element
        self.dc_output_current = CurrentMeasurement(output.dc_section, output.dc_name)
        self.mirror_ratio = float(semantic_mapping.mirror_ratio)
        self.vout_nominal = float(semantic_mapping.vout_nominal)

    @property
    def runtime_parameters(self) -> tuple[RuntimeParameter, ...]:
        return self._runtime_parameters

    @property
    def reference_excitation_quantity(self) -> str | None:
        """Quantity used only by the functional-test perturbation policy."""

        excitation = self._semantic_mapping.reference_excitation
        if excitation is not None and excitation.status == "resolved":
            return excitation.excitation_type
        if (
            self._semantic_mapping.reference_current_measurement.measurement_type
            == "source_current"
        ):
            return "current"
        return None

    def scale_reference(
        self,
        params: Mapping[str, float],
        scale: float,
    ) -> Mapping[str, float]:
        """Scale the Stage A bias-current variables that establish the mirror."""

        factor = float(scale)
        if not math.isfinite(factor) or factor <= 0.0:
            raise ValueError("reference scale must be positive and finite")
        scaled = dict(params)
        for name in self._semantic_mapping.scale_parameter_names:
            scaled[name] = params[name] * factor
        return scaled

    def prepare_reference_stimulus(
        self,
        params: Mapping[str, float],
        scale: float,
    ) -> Mapping[str, Any]:
        """Resolve one external excitation point without changing optimizer schema."""

        factor = float(scale)
        scaled_params = self.scale_reference(params, factor)
        scale_names = self._semantic_mapping.scale_parameter_names
        measurement = self._semantic_mapping.reference_current_measurement
        excitation = self._semantic_mapping.reference_excitation

        if excitation is None:
            if measurement.measurement_type != "source_current":
                raise ReferenceStimulusUnavailableError(
                    f"{self.name}: reference excitation semantic is unavailable"
                )
            excitation_status = "resolved"
            excitation_element = measurement.element
            excitation_quantity = "current"
            excitation_nominal = None
        else:
            excitation_status = excitation.status
            excitation_element = excitation.element
            excitation_quantity = excitation.excitation_type
            excitation_nominal = excitation.nominal
        if (
            excitation_status != "resolved"
            or excitation_element is None
            or excitation_quantity is None
        ):
            reason = None if excitation is None else excitation.reason
            raise ReferenceStimulusUnavailableError(
                f"{self.name}: reference excitation is {excitation_status}: "
                f"{reason or 'no uniquely controllable external source'}"
            )

        if scale_names:
            circuit = self.build_circuit(scaled_params, self.vout_nominal)
            try:
                requested_value = float(circuit[excitation_element].dc_value)
            except Exception as error:
                raise BindingValidationError(
                    f"{self.name}: cannot resolve scaled excitation source "
                    f"{excitation_element!r}"
                ) from error
            return {
                "params": scaled_params,
                "stimulus_overrides": None,
                "excitation_element": excitation_element,
                "excitation_quantity": excitation_quantity,
                "nominal_excitation": (
                    abs(float(excitation_nominal))
                    if excitation_nominal is not None
                    else abs(requested_value / factor)
                ),
                "requested_excitation": abs(requested_value),
                "requested_reference": (
                    abs(requested_value)
                    if measurement.measurement_type == "source_current"
                    and excitation_element == measurement.element
                    else None
                ),
            }

        nominal_circuit = self.build_circuit(params, self.vout_nominal)
        try:
            nominal_source = nominal_circuit[excitation_element]
            nominal_value = float(nominal_source.dc_value)
        except Exception as error:
            raise BindingValidationError(
                f"{self.name}: cannot resolve fixed reference stimulus "
                f"{excitation_element!r}"
            ) from error
        if not math.isfinite(nominal_value) or nominal_value == 0.0:
            raise BindingValidationError(
                f"{self.name}: fixed reference stimulus {excitation_element!r} "
                "must be finite and non-zero"
            )

        requested_value = nominal_value * factor
        return {
            "params": dict(params),
            "stimulus_overrides": {
                excitation_element: {
                    "quantity": excitation_quantity,
                    "value": requested_value,
                }
            },
            "excitation_element": excitation_element,
            "excitation_quantity": excitation_quantity,
            "nominal_excitation": abs(nominal_value),
            "requested_excitation": abs(requested_value),
            "requested_reference": (
                abs(requested_value)
                if measurement.measurement_type == "source_current"
                and excitation_element == measurement.element
                else None
            ),
        }

    def read_applied_stimulus(
        self,
        circuit: Any,
        prepared: Mapping[str, Any],
    ) -> float:
        """Read back the independent source value after circuit construction."""

        try:
            value = float(circuit[prepared["excitation_element"]].dc_value)
        except Exception as error:
            raise BindingValidationError(
                f"{self.name}: cannot verify applied reference excitation"
            ) from error
        if not math.isfinite(value):
            raise BindingValidationError(
                f"{self.name}: applied reference excitation is not finite"
            )
        return abs(value)

    def build_circuit(
        self,
        params: Mapping[str, float],
        vout_nominal: float,
        stimulus_overrides: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> Any:
        """CurrentMirror-compatible facade over the generic candidate builder."""

        return self.candidate_builder.build_circuit(
            params,
            argument_overrides={
                self._semantic_mapping.output_parameter: float(vout_nominal)
            },
            stimulus_overrides=stimulus_overrides,
        )

    def validate_nominal_bindings(self) -> Any:
        """Build the nominal point and validate every required runtime binding."""

        nominal = {item.name: item.nominal for item in self._runtime_parameters}
        return self.build_circuit(nominal, self.vout_nominal)
