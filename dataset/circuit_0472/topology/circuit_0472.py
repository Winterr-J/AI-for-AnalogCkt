"""Generated from circuit_0472.cir by cadence_to_pyspice.py.

All values absent from the source netlist remain required variables.
"""

from __future__ import annotations

from typing import Any

from PySpice.Spice.Netlist import Circuit


def build_circuit(
    *,
    VDD: Any,
    IOUT1: Any,
    IB1: Any,
    NPN_MODEL: Any,
    R0: Any,
    R1: Any,
) -> Circuit:
    circuit = Circuit("AnalogGenie current mirrors 608 (renumbered circuit_0472)")

    # VSS is treated as the PySpice ground node. Other source values
    # are caller-supplied variables because the dataset has no values.
    circuit.V("VDD", "VDD", circuit.gnd, VDD)
    circuit.V("IOUT1", "IOUT1", circuit.gnd, IOUT1)
    circuit.I("IB1", circuit.gnd, "IB1", IB1)

    # Model names, W/L dimensions, and passive values are variables.
    circuit.raw_spice += "Q2 VDD IB1 net02 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q1 IB1 net02 net04 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q0 IOUT1 net02 net05 0 " + str(NPN_MODEL) + "\n"
    circuit.R("0", "net04", circuit.gnd, R0)
    circuit.R("1", "net05", circuit.gnd, R1)
    return circuit
