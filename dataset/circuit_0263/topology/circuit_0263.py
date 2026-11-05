"""Generated from circuit_0263.cir by cadence_to_pyspice.py.

All values absent from the source netlist remain required variables.
"""

from __future__ import annotations

from typing import Any

from PySpice.Spice.Netlist import Circuit


def build_circuit(
    *,
    VIN1: Any,
    VIN2: Any,
    VCLK1: Any,
    NMOS_MODEL: Any,
    W_M2: Any,
    L_M2: Any,
    W_M1: Any,
    L_M1: Any,
    C1: Any,
    C0: Any,
) -> Circuit:
    circuit = Circuit("AnalogGenie intro to switched capacitor circuits 389 (renumbered circuit_0263)")

    # VSS is treated as the PySpice ground node. Other source values
    # are caller-supplied variables because the dataset has no values.
    circuit.V("VIN1", "VIN1", circuit.gnd, VIN1)
    circuit.V("VIN2", "VIN2", circuit.gnd, VIN2)
    circuit.V("VCLK1", "VCLK1", circuit.gnd, VCLK1)

    # Model names, W/L dimensions, and passive values are variables.
    circuit.MOSFET("2", "VIN2", "VCLK1", "VOUT2", circuit.gnd, model=NMOS_MODEL, width=W_M2, length=L_M2)
    circuit.MOSFET("1", "VIN1", "VCLK1", "VOUT1", circuit.gnd, model=NMOS_MODEL, width=W_M1, length=L_M1)
    circuit.C("1", "VOUT2", circuit.gnd, C1)
    circuit.C("0", "VOUT1", circuit.gnd, C0)
    return circuit
