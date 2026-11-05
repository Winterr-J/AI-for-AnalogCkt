"""Generated from circuit_0605.cir by cadence_to_pyspice.py.

All values absent from the source netlist remain required variables.
"""

from __future__ import annotations

from typing import Any

from PySpice.Spice.Netlist import Circuit


def build_circuit(
    *,
    VDD: Any,
    VIN1: Any,
    VCLK1: Any,
    NMOS_MODEL: Any,
    C0: Any,
    W_M1: Any,
    L_M1: Any,
    W_M0: Any,
    L_M0: Any,
    R0: Any,
) -> Circuit:
    circuit = Circuit("AnalogGenie 750 / circuit_0605")

    # VSS is treated as the PySpice ground node. Other source values
    # are caller-supplied variables because the dataset has no values.
    circuit.V("VDD", "VDD", circuit.gnd, VDD)
    circuit.V("VIN1", "VIN1", circuit.gnd, VIN1)
    circuit.V("VCLK1", "VCLK1", circuit.gnd, VCLK1)

    # Model names, W/L dimensions, and passive values are variables.
    circuit.C("0", "net3", circuit.gnd, C0)
    circuit.MOSFET("1", "VDD", "net3", "VOUT1", circuit.gnd, model=NMOS_MODEL, width=W_M1, length=L_M1)
    circuit.MOSFET("0", "VIN1", "VCLK1", "net3", circuit.gnd, model=NMOS_MODEL, width=W_M0, length=L_M0)
    circuit.R("0", "VOUT1", circuit.gnd, R0)
    return circuit
