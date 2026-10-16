"""Automatic Stage A/B to Active Learning circuit onboarding."""

from .auto_config import (
    build_active_learning_circuit_config,
    resolve_nominal_values,
    resolve_reference_excitation_role,
    resolve_reference_current_role,
)
from .errors import (
    AutoConfigError,
    BindingValidationError,
    ReferenceStimulusUnavailableError,
    SemanticMappingError,
)
from .generic_adapter import GenericPySpiceAdapter, GenericPySpiceBuilder
from .generic_testbench import (
    FeedbackEdgeRequest,
    GenericTestbenchDecorator,
    LoadAttachment,
    NativeOperatingPoint,
    SourceSpecification,
    TestbenchBuild,
    TestbenchMetadata,
    capture_native_operating_point,
    derive_feedback_dc_offset,
    native_op_nodeset,
    probe_differential_voltage,
    probe_node_voltage,
    probe_voltage_source_branch_current,
    run_decorated_operating_point,
    run_native_operating_point,
)
from .models import (
    ActiveLearningCircuitConfig,
    OpAmpSemanticMapping,
    SemanticCurrentMeasurement,
    SemanticReferenceExcitation,
    SemanticTerminalBinding,
)
from .opamp_semantics import (
    build_opamp_semantic_mapping,
    load_opamp_semantic_mapping,
    resolve_differential_input_polarity,
    resolve_opamp_semantics,
)
from .runtime_provider import (
    CircuitTypeRuntimeProvider,
    registered_circuit_types,
    resolve_runtime_provider,
)

__all__ = [
    "ActiveLearningCircuitConfig",
    "AutoConfigError",
    "BindingValidationError",
    "CircuitTypeRuntimeProvider",
    "FeedbackEdgeRequest",
    "GenericPySpiceAdapter",
    "GenericPySpiceBuilder",
    "GenericTestbenchDecorator",
    "LoadAttachment",
    "NativeOperatingPoint",
    "OpAmpSemanticMapping",
    "ReferenceStimulusUnavailableError",
    "SemanticMappingError",
    "SemanticCurrentMeasurement",
    "SemanticReferenceExcitation",
    "SemanticTerminalBinding",
    "SourceSpecification",
    "TestbenchBuild",
    "TestbenchMetadata",
    "build_opamp_semantic_mapping",
    "build_active_learning_circuit_config",
    "capture_native_operating_point",
    "derive_feedback_dc_offset",
    "load_opamp_semantic_mapping",
    "native_op_nodeset",
    "probe_differential_voltage",
    "probe_node_voltage",
    "probe_voltage_source_branch_current",
    "resolve_differential_input_polarity",
    "resolve_nominal_values",
    "resolve_opamp_semantics",
    "resolve_runtime_provider",
    "resolve_reference_excitation_role",
    "resolve_reference_current_role",
    "run_decorated_operating_point",
    "run_native_operating_point",
    "registered_circuit_types",
]
