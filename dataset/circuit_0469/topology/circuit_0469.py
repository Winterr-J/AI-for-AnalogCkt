"""Generated from circuit_0469.cir by cadence_to_pyspice.py.

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
    R1: Any,
    R0: Any,
) -> Circuit:
    circuit = Circuit("AnalogGenie current mirrors 605 (renumbered circuit_0469)")

    # VSS is treated as the PySpice ground node. Other source values
    # are caller-supplied variables because the dataset has no values.
    circuit.V("IOUT1", "IOUT1", circuit.gnd, IOUT1)
    circuit.I("IB1", circuit.gnd, "IB1", IB1)

    # Model names, W/L dimensions, and passive values are variables.
    circuit.raw_spice += "Q0 IOUT1 IB1 net3 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q1 IB1 IB1 net7 0 " + str(NPN_MODEL) + "\n"
    circuit.R("1", "net3", circuit.gnd, R1)
    circuit.R("0", "net7", circuit.gnd, R0)
    return circuit
