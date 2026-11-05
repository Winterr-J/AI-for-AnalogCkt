"""Generated from circuit_0617.cir by cadence_to_pyspice.py.

All values absent from the source netlist remain required variables.
"""

from __future__ import annotations

from typing import Any

from PySpice.Spice.Netlist import Circuit


def build_circuit(
    *,
    VDD: Any,
    VIN1: Any,
    IB1: Any,
    IB2: Any,
    IB3: Any,
    VHOLD1: Any,
    VTRACK1: Any,
    PMOS_MODEL: Any,
    PNP_MODEL: Any,
    DIODE_MODEL: Any,
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
    C1: Any,
    C0: Any,
    R2: Any,
    R1: Any,
    R0: Any,
) -> Circuit:
    circuit = Circuit("AnalogGenie 763 / circuit_0617")

    # VSS is treated as the PySpice ground node. Other source values
    # are caller-supplied variables because the dataset has no values.
    circuit.V("VDD", "VDD", circuit.gnd, VDD)
    circuit.V("VIN1", "VIN1", circuit.gnd, VIN1)
    circuit.I("IB1", circuit.gnd, "IB1", IB1)
    circuit.I("IB2", circuit.gnd, "IB2", IB2)
    circuit.I("IB3", circuit.gnd, "IB3", IB3)
    circuit.V("VHOLD1", "VHOLD1", circuit.gnd, VHOLD1)
    circuit.V("VTRACK1", "VTRACK1", circuit.gnd, VTRACK1)

    # Model names, W/L dimensions, and passive values are variables.
    circuit.raw_spice += "Q6 VOUT1 IB1 VDD 0 " + str(PNP_MODEL) + "\n"
    circuit.raw_spice += "Q5 0 net18 net24 0 " + str(PNP_MODEL) + "\n"
    circuit.raw_spice += "Q4 IB1 net24 VDD 0 " + str(PNP_MODEL) + "\n"
    circuit.raw_spice += "Q3 0 net19 net18 0 " + str(PNP_MODEL) + "\n"
    circuit.raw_spice += "Q2 0 net19 net19 0 " + str(PNP_MODEL) + "\n"
    circuit.raw_spice += "Q1 IB2 VTRACK1 net12 0 " + str(PNP_MODEL) + "\n"
    circuit.raw_spice += "Q0 IB2 VHOLD1 net11 0 " + str(PNP_MODEL) + "\n"
    circuit.MOSFET("11", "net18", "net16", "net20", "VDD", model=PMOS_MODEL, width=W_M11, length=L_M11)
    circuit.MOSFET("10", "net19", "IB1", "net20", "VDD", model=PMOS_MODEL, width=W_M10, length=L_M10)
    circuit.MOSFET("9", "net24", "IB3", "net33", "VDD", model=PMOS_MODEL, width=W_M9, length=L_M9)
    circuit.MOSFET("8", "net33", "net15", "VDD", "VDD", model=PMOS_MODEL, width=W_M8, length=L_M8)
    circuit.MOSFET("7", "net34", "net15", "VDD", "VDD", model=PMOS_MODEL, width=W_M7, length=L_M7)
    circuit.MOSFET("6", "net20", "IB3", "net34", "VDD", model=PMOS_MODEL, width=W_M6, length=L_M6)
    circuit.MOSFET("5", "net12", "IB3", "net35", "VDD", model=PMOS_MODEL, width=W_M5, length=L_M5)
    circuit.MOSFET("4", "net35", "net15", "VDD", "VDD", model=PMOS_MODEL, width=W_M4, length=L_M4)
    circuit.MOSFET("3", "net38", "net15", "VDD", "VDD", model=PMOS_MODEL, width=W_M3, length=L_M3)
    circuit.MOSFET("2", "net11", "IB3", "net38", "VDD", model=PMOS_MODEL, width=W_M2, length=L_M2)
    circuit.MOSFET("1", "IB3", "IB3", "net15", "VDD", model=PMOS_MODEL, width=W_M1, length=L_M1)
    circuit.MOSFET("0", "net15", "net15", "VDD", "VDD", model=PMOS_MODEL, width=W_M0, length=L_M0)
    circuit.D("5", "net32", "net11", model=DIODE_MODEL)
    circuit.D("4", "net12", "net36", model=DIODE_MODEL)
    circuit.D("3", "net16", "net12", model=DIODE_MODEL)
    circuit.D("2", "VIN1", "net12", model=DIODE_MODEL)
    circuit.D("1", "net11", "net16", model=DIODE_MODEL)
    circuit.D("0", "net11", "VIN1", model=DIODE_MODEL)
    circuit.C("1", "net24", "net18", C1)
    circuit.C("0", "net16", circuit.gnd, C0)
    circuit.R("2", "VOUT1", circuit.gnd, R2)
    circuit.R("1", "net32", "IB1", R1)
    circuit.R("0", "net36", "IB1", R0)
    return circuit
