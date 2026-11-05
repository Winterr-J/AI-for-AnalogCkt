"""Generated from circuit_0607.cir by cadence_to_pyspice.py.

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
    VCLK2: Any,
    NMOS_MODEL: Any,
    R0: Any,
    C0: Any,
    W_M2: Any,
    L_M2: Any,
    W_M0: Any,
    L_M0: Any,
    W_M1: Any,
    L_M1: Any,
) -> Circuit:
    circuit = Circuit("AnalogGenie 752 / circuit_0607")

    # VSS is treated as the PySpice ground node. Other source values
    # are caller-supplied variables because the dataset has no values.
    circuit.V("VDD", "VDD", circuit.gnd, VDD)
    circuit.V("VIN1", "VIN1", circuit.gnd, VIN1)
    circuit.V("VCLK1", "VCLK1", circuit.gnd, VCLK1)
    circuit.V("VCLK2", "VCLK2", circuit.gnd, VCLK2)

    # Model names, W/L dimensions, and passive values are variables.
    circuit.R("0", "VOUT1", circuit.gnd, R0)
    circuit.C("0", "net11", circuit.gnd, C0)
    circuit.MOSFET("2", "net11", "VCLK2", "net11", circuit.gnd, model=NMOS_MODEL, width=W_M2, length=L_M2)
    circuit.MOSFET("0", "VIN1", "VCLK1", "net11", circuit.gnd, model=NMOS_MODEL, width=W_M0, length=L_M0)
    circuit.MOSFET("1", "VDD", "net11", "VOUT1", circuit.gnd, model=NMOS_MODEL, width=W_M1, length=L_M1)
    return circuit
