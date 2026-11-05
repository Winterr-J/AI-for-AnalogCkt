"""Generated from circuit_0262.cir by cadence_to_pyspice.py.

All values absent from the source netlist remain required variables.
"""

from __future__ import annotations

from typing import Any

from PySpice.Spice.Netlist import Circuit


def build_circuit(
    *,
    VIN1: Any,
    VCLK1: Any,
    VCLK2: Any,
    NMOS_MODEL: Any,
    W_M4: Any,
    L_M4: Any,
    W_M3: Any,
    L_M3: Any,
    C0: Any,
) -> Circuit:
    circuit = Circuit("AnalogGenie intro to switched capacitor circuits 388 (renumbered circuit_0262)")

    # VSS is treated as the PySpice ground node. Other source values
    # are caller-supplied variables because the dataset has no values.
    circuit.V("VIN1", "VIN1", circuit.gnd, VIN1)
    circuit.V("VCLK1", "VCLK1", circuit.gnd, VCLK1)
    circuit.V("VCLK2", "VCLK2", circuit.gnd, VCLK2)

    # Model names, W/L dimensions, and passive values are variables.
    circuit.MOSFET("4", "VOUT1", "VCLK2", "VOUT1", circuit.gnd, model=NMOS_MODEL, width=W_M4, length=L_M4)
    circuit.MOSFET("3", "VIN1", "VCLK1", "VOUT1", circuit.gnd, model=NMOS_MODEL, width=W_M3, length=L_M3)
    circuit.C("0", "VOUT1", circuit.gnd, C0)
    return circuit
