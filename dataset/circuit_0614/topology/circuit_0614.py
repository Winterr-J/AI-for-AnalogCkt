"""Generated from circuit_0614.cir by cadence_to_pyspice.py.

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
    VB2: Any,
    VB3: Any,
    IB1: Any,
    VCLK1: Any,
    VCLK2: Any,
    VCLK3: Any,
    NMOS_MODEL: Any,
    PMOS_MODEL: Any,
    NPN_MODEL: Any,
    PNP_MODEL: Any,
    W_M28: Any,
    L_M28: Any,
    W_M27: Any,
    L_M27: Any,
    W_M26: Any,
    L_M26: Any,
    W_M24: Any,
    L_M24: Any,
    W_M20: Any,
    L_M20: Any,
    W_M18: Any,
    L_M18: Any,
    W_M14: Any,
    L_M14: Any,
    W_M13: Any,
    L_M13: Any,
    W_M12: Any,
    L_M12: Any,
    W_M11: Any,
    L_M11: Any,
    W_M8: Any,
    L_M8: Any,
    W_M1: Any,
    L_M1: Any,
    W_M0: Any,
    L_M0: Any,
    W_M25: Any,
    L_M25: Any,
    W_M23: Any,
    L_M23: Any,
    W_M22: Any,
    L_M22: Any,
    W_M21: Any,
    L_M21: Any,
    W_M19: Any,
    L_M19: Any,
    W_M17: Any,
    L_M17: Any,
    W_M16: Any,
    L_M16: Any,
    W_M15: Any,
    L_M15: Any,
    W_M10: Any,
    L_M10: Any,
    W_M9: Any,
    L_M9: Any,
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
    R14: Any,
    R13: Any,
    R12: Any,
    R11: Any,
    R10: Any,
    R9: Any,
    R8: Any,
    R7: Any,
    R6: Any,
    R5: Any,
    R4: Any,
    R3: Any,
    R2: Any,
    R1: Any,
    R0: Any,
    C2: Any,
    C1: Any,
    C0: Any,
) -> Circuit:
    circuit = Circuit("AnalogGenie 760 / circuit_0614")

    # VSS is treated as the PySpice ground node. Other source values
    # are caller-supplied variables because the dataset has no values.
    circuit.V("VDD", "VDD", circuit.gnd, VDD)
    circuit.V("VIN1", "VIN1", circuit.gnd, VIN1)
    circuit.V("VB1", "VB1", circuit.gnd, VB1)
    circuit.V("VB2", "VB2", circuit.gnd, VB2)
    circuit.V("VB3", "VB3", circuit.gnd, VB3)
    circuit.I("IB1", circuit.gnd, "IB1", IB1)
    circuit.V("VCLK1", "VCLK1", circuit.gnd, VCLK1)
    circuit.V("VCLK2", "VCLK2", circuit.gnd, VCLK2)
    circuit.V("VCLK3", "VCLK3", circuit.gnd, VCLK3)

    # Model names, W/L dimensions, and passive values are variables.
    circuit.raw_spice += "Q23 net0131 net0131 net0129 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q22 net028 net028 net0131 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q20 net031 net0112 net028 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q19 VDD net026 net096 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q17 net026 net026 net077 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q16 VDD net0126 net098 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q13 net034 net034 net033 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q12 VOUT1 VOUT1 net034 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q10 net032 net065 VOUT1 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q9 VDD net027 net054 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q7 net027 net027 net049 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q6 VDD net024 net056 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q4 net29 net23 net30 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q3 net16 net23 net31 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q2 net36 net6 net32 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q1 net6 net6 net33 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q0 net3 net23 net34 0 " + str(NPN_MODEL) + "\n"
    circuit.MOSFET("28", "net075", "VCLK3", "VOUT1", circuit.gnd, model=NMOS_MODEL, width=W_M28, length=L_M28)
    circuit.MOSFET("27", "VIN1", "VCLK2", "net075", circuit.gnd, model=NMOS_MODEL, width=W_M27, length=L_M27)
    circuit.MOSFET("26", "net0129", "VB3", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M26, length=L_M26)
    circuit.MOSFET("24", "net077", "VB2", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M24, length=L_M24)
    circuit.MOSFET("20", "net033", "VB3", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M20, length=L_M20)
    circuit.MOSFET("18", "net049", "VB2", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M18, length=L_M18)
    circuit.MOSFET("14", "net028", "VCLK2", "net030", circuit.gnd, model=NMOS_MODEL, width=W_M14, length=L_M14)
    circuit.MOSFET("13", "net39", "VCLK1", "net35", circuit.gnd, model=NMOS_MODEL, width=W_M13, length=L_M13)
    circuit.MOSFET("12", "net40", "VCLK1", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M12, length=L_M12)
    circuit.MOSFET("11", "IB1", "IB1", "net29", circuit.gnd, model=NMOS_MODEL, width=W_M11, length=L_M11)
    circuit.MOSFET("8", "net35", "IB1", "net36", circuit.gnd, model=NMOS_MODEL, width=W_M8, length=L_M8)
    circuit.MOSFET("1", "net24", "net39", "net3", circuit.gnd, model=NMOS_MODEL, width=W_M1, length=L_M1)
    circuit.MOSFET("0", "net22", "net40", "net3", circuit.gnd, model=NMOS_MODEL, width=W_M0, length=L_M0)
    circuit.MOSFET("25", "net031", "VB1", "VDD", "VDD", model=PMOS_MODEL, width=W_M25, length=L_M25)
    circuit.MOSFET("23", "net026", "net077", "net098", "VDD", model=PMOS_MODEL, width=W_M23, length=L_M23)
    circuit.MOSFET("22", "net099", "net075", "net0126", "VDD", model=PMOS_MODEL, width=W_M22, length=L_M22)
    circuit.MOSFET("21", "net0126", "VB1", "VDD", "VDD", model=PMOS_MODEL, width=W_M21, length=L_M21)
    circuit.MOSFET("19", "net032", "VB1", "VDD", "VDD", model=PMOS_MODEL, width=W_M19, length=L_M19)
    circuit.MOSFET("17", "net027", "net049", "net056", "VDD", model=PMOS_MODEL, width=W_M17, length=L_M17)
    circuit.MOSFET("16", "net057", "net030", "net024", "VDD", model=PMOS_MODEL, width=W_M16, length=L_M16)
    circuit.MOSFET("15", "net024", "VB1", "VDD", "VDD", model=PMOS_MODEL, width=W_M15, length=L_M15)
    circuit.MOSFET("10", "net12", "net12", "VDD", "VDD", model=PMOS_MODEL, width=W_M10, length=L_M10)
    circuit.MOSFET("9", "net16", "net16", "net12", "VDD", model=PMOS_MODEL, width=W_M9, length=L_M9)
    circuit.MOSFET("7", circuit.gnd, "net16", "net25", "VDD", model=PMOS_MODEL, width=W_M7, length=L_M7)
    circuit.MOSFET("6", "net25", "net12", "VDD", "VDD", model=PMOS_MODEL, width=W_M6, length=L_M6)
    circuit.MOSFET("5", "net24", "net12", "VDD", "VDD", model=PMOS_MODEL, width=W_M5, length=L_M5)
    circuit.MOSFET("4", "net35", "net25", "net24", "VDD", model=PMOS_MODEL, width=W_M4, length=L_M4)
    circuit.MOSFET("3", "net22", "net12", "VDD", "VDD", model=PMOS_MODEL, width=W_M3, length=L_M3)
    circuit.MOSFET("2", "net6", "net25", "net22", "VDD", model=PMOS_MODEL, width=W_M2, length=L_M2)
    circuit.R("14", "net028", "net095", R14)
    circuit.R("13", "net079", "net028", R13)
    circuit.R("12", "net079", "net0112", R12)
    circuit.R("11", "net096", "net0129", R11)
    circuit.R("10", "net031", "net097", R10)
    circuit.R("9", "VOUT1", "net053", R9)
    circuit.R("8", "net052", "VOUT1", R8)
    circuit.R("7", "net052", "net065", R7)
    circuit.R("6", "net054", "net033", R6)
    circuit.R("5", "net032", "net055", R5)
    circuit.R("4", "net30", circuit.gnd, R4)
    circuit.R("3", "net31", circuit.gnd, R3)
    circuit.R("2", "net32", circuit.gnd, R2)
    circuit.R("1", "net33", circuit.gnd, R1)
    circuit.R("0", "net34", circuit.gnd, R0)
    circuit.C("2", "net030", "net35", C2)
    circuit.C("1", "net028", "net39", C1)
    circuit.C("0", "net40", circuit.gnd, C0)
    circuit.raw_spice += "Q24 0 net0129 net095 0 " + str(PNP_MODEL) + "\n"
    circuit.raw_spice += "Q21 net079 net031 VDD 0 " + str(PNP_MODEL) + "\n"
    circuit.raw_spice += "Q18 0 net026 net097 0 " + str(PNP_MODEL) + "\n"
    circuit.raw_spice += "Q15 0 net026 net099 0 " + str(PNP_MODEL) + "\n"
    circuit.raw_spice += "Q14 0 net033 net053 0 " + str(PNP_MODEL) + "\n"
    circuit.raw_spice += "Q11 net052 net032 VDD 0 " + str(PNP_MODEL) + "\n"
    circuit.raw_spice += "Q8 0 net027 net055 0 " + str(PNP_MODEL) + "\n"
    circuit.raw_spice += "Q5 0 net027 net057 0 " + str(PNP_MODEL) + "\n"
    return circuit
