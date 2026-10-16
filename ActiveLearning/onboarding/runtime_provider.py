"""Circuit-type runtime-provider registry for Active Learning onboarding.

A future circuit type is integrated by registering one provider containing its
Stage A/B structural builder, semantic/evaluator/validator factories, template
name, and testbench defaults.  Callers resolve that provider once instead of
adding circuit-type conditionals throughout ``main`` and the runtime models.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any, Callable, Mapping

from .errors import AutoConfigError


@dataclass(frozen=True)
class CircuitTypeRuntimeProvider:
    """One registered circuit type's structural and executable components."""

    circuit_type: str
    semantic_resolver_name: str
    evaluator_name: str
    functional_validator_name: str
    performance_template_name: str
    testbench_defaults: Mapping[str, Any]
    structural_builder_name: str
    evaluator_factory: Callable[[Any, Mapping[str, Any] | None], Any]
    validator_factory: Callable[[Any], Any]
    nominal_smoke_factory: Callable[
        [Any, Mapping[str, Any] | None, Mapping[str, Any] | None],
        dict[str, Any],
    ] | None = None

    def build_config(
        self,
        circuit_id: str,
        *,
        repository_root: str | Path | None = None,
    ) -> Any:
        # The local import keeps the registry independent of the sizeable
        # Stage A/B implementation and prevents onboarding import cycles.
        from . import auto_config

        builder = getattr(auto_config, self.structural_builder_name)
        return builder(
            self.circuit_type,
            circuit_id,
            repository_root=repository_root,
            runtime_provider=self,
        )

    def performance_template_path(self, active_learning_root: Path) -> Path:
        return (
            active_learning_root
            / "circuit_types"
            / self.performance_template_name
            / "performance_template.yaml"
        )

    def create_evaluator(
        self,
        runtime: Any,
        *,
        testbench_overrides: Mapping[str, Any] | None = None,
    ) -> Any:
        return self.evaluator_factory(runtime, testbench_overrides)

    def create_functional_validator(self, runtime: Any) -> Any:
        return self.validator_factory(runtime)

    def evaluate_nominal(
        self,
        runtime: Any,
        *,
        target_specs: Mapping[str, Any] | None = None,
        testbench_overrides: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.nominal_smoke_factory is None:
            raise AutoConfigError(
                f"No normalized nominal smoke path registered for {self.circuit_type!r}"
            )
        return self.nominal_smoke_factory(runtime, target_specs, testbench_overrides)


def _deep_merge(
    base: Mapping[str, Any],
    override: Mapping[str, Any],
) -> dict[str, Any]:
    result = deepcopy(dict(base))
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _current_mirror_evaluator_factory(
    runtime: Any,
    _testbench_overrides: Mapping[str, Any] | None,
) -> Any:
    from ActiveLearning.InitializeD0.evaluator import evaluate_current_mirror

    return partial(evaluate_current_mirror, adapter=runtime.adapter)


def _current_mirror_validator_factory(runtime: Any) -> Any:
    from ActiveLearning.InitializeD0.validation import (
        check_current_mirror_functionality,
    )

    return partial(check_current_mirror_functionality, adapter=runtime.adapter)


def _opamp_evaluator_factory(
    runtime: Any,
    testbench_overrides: Mapping[str, Any] | None,
) -> Any:
    from ActiveLearning.InitializeD0.evaluator.opamp_evaluator import OpAmpEvaluator

    config = _deep_merge(runtime.testbench_config, testbench_overrides or {})
    load = config["load"]
    ac = config["ac"]
    semantic = replace(
        runtime.semantic_mapping,
        load_definition=replace(
            runtime.semantic_mapping.load_definition,
            preserve_native=bool(load["preserve_native_elements"]),
            capacitance_f=(
                None
                if load.get("capacitance_f") is None
                else float(load["capacitance_f"])
            ),
            resistance_ohm=(
                None
                if load.get("resistance_ohm") is None
                else float(load["resistance_ohm"])
            ),
        ),
    )
    return OpAmpEvaluator(
        name=runtime.circuit_id,
        candidate_builder=runtime.candidate_builder,
        semantic_mapping=semantic,
        temperature_c=float(config.get("temperature_c", 25.0)),
        ac_start_hz=float(ac["start_hz"]),
        ac_stop_hz=float(ac["stop_hz"]),
        points_per_decade=int(ac["points_per_decade"]),
    )


def _opamp_validator_factory(runtime: Any) -> Any:
    from ActiveLearning.InitializeD0.validation.opamp_functional_validator import (
        OpAmpFunctionalValidator,
    )

    return OpAmpFunctionalValidator(
        parameter_template=runtime.param_template,
        semantic_mapping=runtime.semantic_mapping,
    )


def _opamp_nominal_smoke_factory(
    runtime: Any,
    target_specs: Mapping[str, Any] | None,
    testbench_overrides: Mapping[str, Any] | None,
) -> dict[str, Any]:
    params = {item.name: item.nominal for item in runtime.runtime_parameters}
    evaluator = _opamp_evaluator_factory(runtime, testbench_overrides)
    evaluation = evaluator.evaluate(params)
    validation = _opamp_validator_factory(runtime).validate(
        params,
        evaluation,
        target_specs=target_specs,
    )
    return {
        "params": params,
        "evaluation": evaluation,
        "validation": validation,
    }


_PROVIDERS = {
    "CurrentMirror": CircuitTypeRuntimeProvider(
        circuit_type="CurrentMirror",
        semantic_resolver_name="CurrentMirrorAutoConfigSemanticResolver",
        evaluator_name="CurrentMirrorEvaluator",
        functional_validator_name="CurrentMirrorFunctionalValidator",
        performance_template_name="CurrentMirror",
        testbench_defaults={},
        structural_builder_name="_build_current_mirror_circuit_config",
        evaluator_factory=_current_mirror_evaluator_factory,
        validator_factory=_current_mirror_validator_factory,
    ),
    "OpAmp": CircuitTypeRuntimeProvider(
        circuit_type="OpAmp",
        semantic_resolver_name="resolve_opamp_semantics",
        evaluator_name="OpAmpEvaluator",
        functional_validator_name="OpAmpFunctionalValidator",
        performance_template_name="OpAmp",
        testbench_defaults={
            "backend": "Ideal",
            "temperature_c": 25.0,
            "load": {
                "preserve_native_elements": True,
                "capacitance_f": 1.0e-12,
                "resistance_ohm": None,
            },
            "ac": {
                "start_hz": 1.0,
                "stop_hz": 1.0e9,
                "points_per_decade": 40,
            },
            "stabilization": {
                "floating_node_anchor_resistance_ohm": 1.0e12,
                "reference_voltage_v": None,
                "reference_source": None,
            },
        },
        structural_builder_name="_build_opamp_circuit_config",
        evaluator_factory=_opamp_evaluator_factory,
        validator_factory=_opamp_validator_factory,
        nominal_smoke_factory=_opamp_nominal_smoke_factory,
    ),
}


def resolve_runtime_provider(circuit_type: str) -> CircuitTypeRuntimeProvider:
    try:
        return _PROVIDERS[str(circuit_type)]
    except KeyError as error:
        raise AutoConfigError(
            f"Unsupported AutoConfig circuit type: {circuit_type}"
        ) from error


def registered_circuit_types() -> tuple[str, ...]:
    return tuple(_PROVIDERS)


__all__ = [
    "CircuitTypeRuntimeProvider",
    "registered_circuit_types",
    "resolve_runtime_provider",
]
