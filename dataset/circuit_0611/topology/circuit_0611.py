"""Generated from circuit_0611.cir by cadence_to_pyspice.py.

All values absent from the source netlist remain required variables.
"""

from __future__ import annotations

from typing import Any

from PySpice.Spice.Netlist import Circuit


def build_circuit(
    *,
    VDD: Any,
    VIN1: Any,
    VB1: Any,
    VCLK1: Any,
    VCLK2: Any,
    NMOS_MODEL: Any,
    PMOS_MODEL: Any,
    W_M15: Any,
    L_M15: Any,
    W_M14: Any,
    L_M14: Any,
    W_M13: Any,
    L_M13: Any,
    W_M12: Any,
    L_M12: Any,
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
    W_M5: Any,
    L_M5: Any,
    W_M6: Any,
    L_M6: Any,
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
    R2: Any,
    R1: Any,
    R0: Any,
    C1: Any,
    C0: Any,
) -> Circuit:
    circuit = Circuit("AnalogGenie 757 / circuit_0611")

    # VSS is treated as the PySpice ground node. Other source values
    # are caller-supplied variables because the dataset has no values.
    circuit.V("VDD", "VDD", circuit.gnd, VDD)
    circuit.V("VIN1", "VIN1", circuit.gnd, VIN1)
    circuit.V("VB1", "VB1", circuit.gnd, VB1)
    circuit.V("VCLK1", "VCLK1", circuit.gnd, VCLK1)
    circuit.V("VCLK2", "VCLK2", circuit.gnd, VCLK2)

    # Model names, W/L dimensions, and passive values are variables.
    circuit.MOSFET("15", "net17", "VCLK1", "net6", circuit.gnd, model=NMOS_MODEL, width=W_M15, length=L_M15)
    circuit.MOSFET("14", "net6", "VCLK2", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M14, length=L_M14)
    circuit.MOSFET("13", "VOUT1", "net26", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M13, length=L_M13)
    circuit.MOSFET("12", "net26", "net15", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M12, length=L_M12)
    circuit.MOSFET("11", "net15", "net15", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M11, length=L_M11)
    circuit.MOSFET("10", "net10", "VB1", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M10, length=L_M10)
    circuit.MOSFET("9", "net21", "net15", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M9, length=L_M9)
    circuit.MOSFET("8", "net24", circuit.gnd, "net21", "net21", model=NMOS_MODEL, width=W_M8, length=L_M8)
    circuit.MOSFET("7", "net19", "net17", "net21", "net21", model=NMOS_MODEL, width=W_M7, length=L_M7)
    circuit.MOSFET("5", "net31", "net31", "net15", "net15", model=NMOS_MODEL, width=W_M5, length=L_M5)
    circuit.MOSFET("6", "VDD", "net24", "net26", "VDD", model=NMOS_MODEL, width=W_M6, length=L_M6)
    circuit.MOSFET("4", "VOUT1", "net24", "VDD", "VDD", model=PMOS_MODEL, width=W_M4, length=L_M4)
    circuit.MOSFET("3", "net19", "net19", "VDD", "VDD", model=PMOS_MODEL, width=W_M3, length=L_M3)
    circuit.MOSFET("2", "net24", "net19", "VDD", "VDD", model=PMOS_MODEL, width=W_M2, length=L_M2)
    circuit.MOSFET("1", "net10", "net10", "VDD", "VDD", model=PMOS_MODEL, width=W_M1, length=L_M1)
    circuit.MOSFET("0", "net31", "net10", "VDD", "VDD", model=PMOS_MODEL, width=W_M0, length=L_M0)
    circuit.R("2", "VIN1", "net6", R2)
    circuit.R("1", "net6", "VOUT1", R1)
    circuit.R("0", "net24", "net41", R0)
    circuit.C("1", "net17", "VOUT1", C1)
    circuit.C("0", "net41", "VOUT1", C0)
    return circuit
