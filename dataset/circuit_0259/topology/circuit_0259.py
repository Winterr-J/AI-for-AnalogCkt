"""Generated from circuit_0259.cir by cadence_to_pyspice.py.

All values absent from the source netlist remain required variables.
"""

from __future__ import annotations

from typing import Any

from PySpice.Spice.Netlist import Circuit


def build_circuit(
    *,
    VIN1: Any,
    VCLK1: Any,
    NMOS_MODEL: Any,
    W_M0: Any,
    L_M0: Any,
    C0: Any,
) -> Circuit:
    circuit = Circuit("AnalogGenie intro to switched capacitor circuits 385 (renumbered circuit_0259)")

    # VSS is treated as the PySpice ground node. Other source values
    # are caller-supplied variables because the dataset has no values.
    circuit.V("VIN1", "VIN1", circuit.gnd, VIN1)
    circuit.V("VCLK1", "VCLK1", circuit.gnd, VCLK1)

    # Model names, W/L dimensions, and passive values are variables.
    circuit.MOSFET("0", "VIN1", "VCLK1", "VOUT1", circuit.gnd, model=NMOS_MODEL, width=W_M0, length=L_M0)
    circuit.C("0", "VOUT1", circuit.gnd, C0)
    return circuit
