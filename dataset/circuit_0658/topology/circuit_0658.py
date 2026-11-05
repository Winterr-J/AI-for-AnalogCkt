"""Generated from circuit_0658.cir by cadence_to_pyspice.py.

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
    PMOS_MODEL: Any,
    W_M5: Any,
    L_M5: Any,
    W_M4: Any,
    L_M4: Any,
    W_M3: Any,
    L_M3: Any,
    W_M2: Any,
    L_M2: Any,
    W_M1: Any,
    L_M1: Any,
    W_M0: Any,
    L_M0: Any,
    W_M11: Any,
    L_M11: Any,
    W_M10: Any,
    L_M10: Any,
    W_M9: Any,
    L_M9: Any,
    W_M8: Any,
    L_M8: Any,
    W_M7: Any,
    L_M7: Any,
    W_M6: Any,
    L_M6: Any,
    C0: Any,
) -> Circuit:
    circuit = Circuit("AnalogGenie 806 / circuit_0658")

    # VSS is treated as the PySpice ground node. Other source values
    # are caller-supplied variables because the dataset has no values.
    circuit.V("VDD", "VDD", circuit.gnd, VDD)
    circuit.V("VIN1", "VIN1", circuit.gnd, VIN1)
    circuit.V("VCLK1", "VCLK1", circuit.gnd, VCLK1)

    # Model names, W/L dimensions, and passive values are variables.
    circuit.MOSFET("5", "net15", "VCLK1", "VDD", "VDD", model=PMOS_MODEL, width=W_M5, length=L_M5)
    circuit.MOSFET("4", "net30", "VCLK1", "VDD", "VDD", model=PMOS_MODEL, width=W_M4, length=L_M4)
    circuit.MOSFET("3", "net30", "net26", "VDD", "VDD", model=PMOS_MODEL, width=W_M3, length=L_M3)
    circuit.MOSFET("2", "net26", "net30", "VDD", "VDD", model=PMOS_MODEL, width=W_M2, length=L_M2)
    circuit.MOSFET("1", "net26", "VCLK1", "VDD", "VDD", model=PMOS_MODEL, width=W_M1, length=L_M1)
    circuit.MOSFET("0", "net25", "VCLK1", "VDD", "VDD", model=PMOS_MODEL, width=W_M0, length=L_M0)
    circuit.MOSFET("11", "VIN1", "net30", "VOUT1", circuit.gnd, model=NMOS_MODEL, width=W_M11, length=L_M11)
    circuit.MOSFET("10", "net14", "VCLK1", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M10, length=L_M10)
    circuit.MOSFET("9", "net15", "VOUT1", "net14", circuit.gnd, model=NMOS_MODEL, width=W_M9, length=L_M9)
    circuit.MOSFET("8", "net25", "VIN1", "net14", circuit.gnd, model=NMOS_MODEL, width=W_M8, length=L_M8)
    circuit.MOSFET("7", "net30", "net26", "net15", circuit.gnd, model=NMOS_MODEL, width=W_M7, length=L_M7)
    circuit.MOSFET("6", "net26", "net30", "net25", circuit.gnd, model=NMOS_MODEL, width=W_M6, length=L_M6)
    circuit.C("0", "VOUT1", circuit.gnd, C0)
    return circuit
