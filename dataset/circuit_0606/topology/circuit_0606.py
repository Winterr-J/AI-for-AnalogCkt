"""Generated from circuit_0606.cir by cadence_to_pyspice.py.

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
    W_M2: Any,
    L_M2: Any,
    W_M0: Any,
    L_M0: Any,
    W_M1: Any,
    L_M1: Any,
    C0: Any,
    R0: Any,
) -> Circuit:
    circuit = Circuit("AnalogGenie 751 / circuit_0606")

    # VSS is treated as the PySpice ground node. Other source values
    # are caller-supplied variables because the dataset has no values.
    circuit.V("VDD", "VDD", circuit.gnd, VDD)
    circuit.V("VIN1", "VIN1", circuit.gnd, VIN1)
    circuit.V("VCLK1", "VCLK1", circuit.gnd, VCLK1)
    circuit.V("VCLK2", "VCLK2", circuit.gnd, VCLK2)

    # Model names, W/L dimensions, and passive values are variables.
    circuit.MOSFET("2", "VIN1", "VCLK2", "net014", circuit.gnd, model=NMOS_MODEL, width=W_M2, length=L_M2)
    circuit.MOSFET("0", "VIN1", "VCLK1", "net014", circuit.gnd, model=NMOS_MODEL, width=W_M0, length=L_M0)
    circuit.MOSFET("1", "VDD", "net014", "VOUT1", "net02", model=NMOS_MODEL, width=W_M1, length=L_M1)
    circuit.C("0", "net014", "net02", C0)
    circuit.R("0", "VOUT1", "net02", R0)
    return circuit
