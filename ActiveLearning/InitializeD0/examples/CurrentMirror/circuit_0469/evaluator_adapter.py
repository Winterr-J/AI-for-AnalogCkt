"""Current Mirror evaluator adapter for circuit_0469."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ActiveLearning.InitializeD0.evaluator.current_mirror_evaluator import (
    CurrentMeasurement,
)

from .topology.circuit_0469 import build_circuit as build_topology


_NPN_MODEL_NAME = "NPN_IDEAL"
_NPN_MODEL_PARAMETERS = {
    "IS": 1.0e-15,
    "BF": 100.0,
    "VAF": 100.0,
    "EG": 1.11,
    "XTI": 3.0,
}


@dataclass(frozen=True)
class Circuit0469CurrentMirrorAdapter:
    """Topology, measurement mappings, and metadata for circuit_0469."""

    name: str = "circuit_0469"
    op_reference_current: CurrentMeasurement = CurrentMeasurement(
        "internal_parameters", "@IIB1[current]"
    )
    op_output_current: CurrentMeasurement = CurrentMeasurement(
        "internal_parameters", "@VIOUT1[i]"
    )
    dc_sweep_source: str = "VIOUT1"
    dc_output_current: CurrentMeasurement = CurrentMeasurement("branches", "viout1")
    mirror_ratio: float = 1.0
    vout_nominal: float = 1.65
    temperature: float = 25.0

    def scale_reference(
        self,
        params: Mapping[str, float],
        scale: float,
    ) -> Mapping[str, float]:
        """Return a copy with the signed circuit_0469 reference current scaled."""

        scaled = dict(params)
        scaled["IB1"] = params["IB1"] * scale
        return scaled

    def build_circuit(self, params: Mapping[str, float], vout_nominal: float) -> Any:
        """Build one configured circuit without modifying the frozen topology."""

        circuit = build_topology(
            IOUT1=vout_nominal,
            IB1=params["IB1"],
            NPN_MODEL=_NPN_MODEL_NAME,
            R0=params["R0"],
            R1=params["R1"],
        )

        # The frozen signed convention defines a negative IB1 value as current
        # injection from VSS into IB1.  Correct only the newly built instance's
        # terminal order; keep the signed parameter value unchanged.
        ib1_source = circuit["IIB1"]
        source_nodes = [str(name).casefold() for name in ib1_source.node_names]
        if source_nodes == ["0", "ib1"]:
            ib1_source.detach()
            circuit.I("IB1", "IB1", circuit.gnd, params["IB1"])
        elif source_nodes != ["ib1", "0"]:
            raise RuntimeError(f"unexpected IIB1 terminals: {ib1_source.node_names!r}")

        circuit.model(_NPN_MODEL_NAME, "NPN", **_NPN_MODEL_PARAMETERS)
        circuit.raw_spice += (
            ".options noacct gmin=1e-12 rshunt=1e12 reltol=1e-4 "
            "abstol=1e-12 vntol=1e-6 method=gear\n"
        )
        return circuit


CIRCUIT_0469_ADAPTER = Circuit0469CurrentMirrorAdapter()

__all__ = ["CIRCUIT_0469_ADAPTER", "Circuit0469CurrentMirrorAdapter"]
