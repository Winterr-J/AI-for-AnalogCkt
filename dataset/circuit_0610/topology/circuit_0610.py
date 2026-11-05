"""Generated from circuit_0610.cir by cadence_to_pyspice.py.

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
    VCLK1: Any,
    VCLK2: Any,
    NMOS_MODEL: Any,
    PMOS_MODEL: Any,
    NPN_MODEL: Any,
    PNP_MODEL: Any,
    R26: Any,
    R25: Any,
    R24: Any,
    R23: Any,
    R22: Any,
    R21: Any,
    R20: Any,
    R19: Any,
    R18: Any,
    R17: Any,
    R16: Any,
    R15: Any,
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
    W_M7: Any,
    L_M7: Any,
    W_M6: Any,
    L_M6: Any,
    W_M5: Any,
    L_M5: Any,
    W_M2: Any,
    L_M2: Any,
    W_M1: Any,
    L_M1: Any,
    W_M0: Any,
    L_M0: Any,
    W_M4: Any,
    L_M4: Any,
    W_M3: Any,
    L_M3: Any,
    C1: Any,
    C0: Any,
) -> Circuit:
    circuit = Circuit("AnalogGenie 755 / circuit_0610")

    # VSS is treated as the PySpice ground node. Other source values
    # are caller-supplied variables because the dataset has no values.
    circuit.V("VDD", "VDD", circuit.gnd, VDD)
    circuit.V("VIN1", "VIN1", circuit.gnd, VIN1)
    circuit.V("VB1", "VB1", circuit.gnd, VB1)
    circuit.V("VB2", "VB2", circuit.gnd, VB2)
    circuit.V("VB3", "VB3", circuit.gnd, VB3)
    circuit.V("VCLK1", "VCLK1", circuit.gnd, VCLK1)
    circuit.V("VCLK2", "VCLK2", circuit.gnd, VCLK2)

    # Model names, W/L dimensions, and passive values are variables.
    circuit.R("26", "net77", "VOUT1", R26)
    circuit.R("25", "net67", "net69", R25)
    circuit.R("24", "net72", "net71", R24)
    circuit.R("23", "net71", circuit.gnd, R23)
    circuit.R("22", "net62", circuit.gnd, R22)
    circuit.R("21", "net57", circuit.gnd, R21)
    circuit.R("20", "net96", circuit.gnd, R20)
    circuit.R("19", "net95", circuit.gnd, R19)
    circuit.R("18", "net94", circuit.gnd, R18)
    circuit.R("17", "VDD", "net97", R17)
    circuit.R("16", "VDD", "net98", R16)
    circuit.R("15", "VDD", "net99", R15)
    circuit.R("14", "net105", circuit.gnd, R14)
    circuit.R("13", "VDD", "net108", R13)
    circuit.R("12", "VDD", "net112", R12)
    circuit.R("11", "net84", circuit.gnd, R11)
    circuit.R("10", "net84", circuit.gnd, R10)
    circuit.R("9", "net36", circuit.gnd, R9)
    circuit.R("8", "net36", circuit.gnd, R8)
    circuit.R("7", "net26", circuit.gnd, R7)
    circuit.R("6", "net111", "net84", R6)
    circuit.R("5", "net103", "net36", R5)
    circuit.R("4", "net104", "net26", R4)
    circuit.R("3", "net106", "net26", R3)
    circuit.R("2", "net110", "net26", R2)
    circuit.R("1", "net113", "net109", R1)
    circuit.R("0", "net101", "net102", R0)
    circuit.raw_spice += "Q30 net91 net62 0 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q29 VDD net61 net62 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q28 net61 net71 net96 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q27 VDD net46 net57 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q26 net61 net57 net95 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q25 net46 net57 net94 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q24 net69 net72 net71 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q23 VOUT1 VOUT1 net69 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q22 net68 net67 net69 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q21 net68 net68 net67 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q20 net68 net77 VOUT1 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q19 VDD net68 net77 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q15 net38 VB2 net105 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q14 net44 net37 net40 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q13 net40 net40 net111 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q12 net37 net40 net103 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q9 net35 VB2 net104 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q8 net30 VB2 net106 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q7 net24 VB2 net110 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q6 VDD VIN1 net24 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q5 VDD VOUT1 net35 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q4 net33 net35 net113 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q3 net28 net24 net101 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q2 VDD VB1 net107 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q31 0 net91 net61 0 " + str(PNP_MODEL) + "\n"
    circuit.raw_spice += "Q18 net68 VB3 net97 0 " + str(PNP_MODEL) + "\n"
    circuit.raw_spice += "Q17 net91 VB3 net98 0 " + str(PNP_MODEL) + "\n"
    circuit.raw_spice += "Q16 net58 VB3 net99 0 " + str(PNP_MODEL) + "\n"
    circuit.raw_spice += "Q0 net33 VB1 net108 0 " + str(PNP_MODEL) + "\n"
    circuit.raw_spice += "Q11 net30 net30 net102 0 " + str(PNP_MODEL) + "\n"
    circuit.raw_spice += "Q10 net30 net30 net109 0 " + str(PNP_MODEL) + "\n"
    circuit.raw_spice += "Q1 net28 VB1 net112 0 " + str(PNP_MODEL) + "\n"
    circuit.MOSFET("7", "net72", "net91", "net68", "net68", model=PMOS_MODEL, width=W_M7, length=L_M7)
    circuit.MOSFET("6", "net61", circuit.gnd, "net58", "net58", model=PMOS_MODEL, width=W_M6, length=L_M6)
    circuit.MOSFET("5", "net46", "net45", "net58", "net58", model=PMOS_MODEL, width=W_M5, length=L_M5)
    circuit.MOSFET("2", "net38", "net38", "net107", "VDD", model=PMOS_MODEL, width=W_M2, length=L_M2)
    circuit.MOSFET("1", "net44", "net38", "net28", "VDD", model=PMOS_MODEL, width=W_M1, length=L_M1)
    circuit.MOSFET("0", "net37", "net38", "net33", "VDD", model=PMOS_MODEL, width=W_M0, length=L_M0)
    circuit.MOSFET("4", "net45", "VCLK1", "net44", circuit.gnd, model=NMOS_MODEL, width=W_M4, length=L_M4)
    circuit.MOSFET("3", "net44", "VCLK2", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M3, length=L_M3)
    circuit.C("1", "VOUT1", "net45", C1)
    circuit.C("0", "net91", "net61", C0)
    return circuit
