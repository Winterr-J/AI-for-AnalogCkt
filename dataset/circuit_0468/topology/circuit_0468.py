"""Generated from circuit_0468.cir by cadence_to_pyspice.py.

All values absent from the source netlist remain required variables.
"""

from __future__ import annotations

from typing import Any

from PySpice.Spice.Netlist import Circuit


def build_circuit(
    *,
    IOUT1: Any,
    IB1: Any,
    NPN_MODEL: Any,
) -> Circuit:
    circuit = Circuit("AnalogGenie current mirrors 604 (renumbered circuit_0468)")

    # VSS is treated as the PySpice ground node. Other source values
    # are caller-supplied variables because the dataset has no values.
    circuit.V("IOUT1", "IOUT1", circuit.gnd, IOUT1)
    circuit.I("IB1", circuit.gnd, "IB1", IB1)

    # Model names, W/L dimensions, and passive values are variables.
    circuit.raw_spice += "Q1 IB1 IB1 0 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q0 IOUT1 IB1 0 0 " + str(NPN_MODEL) + "\n"
    return circuit
