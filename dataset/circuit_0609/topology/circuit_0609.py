"""Generated from circuit_0609.cir by cadence_to_pyspice.py.

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
    VCLK1: Any,
    VCLK2: Any,
    NMOS_MODEL: Any,
    PMOS_MODEL: Any,
    NPN_MODEL: Any,
    PNP_MODEL: Any,
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
    W_M2: Any,
    L_M2: Any,
    W_M1: Any,
    L_M1: Any,
    W_M0: Any,
    L_M0: Any,
    W_M6: Any,
    L_M6: Any,
    W_M5: Any,
    L_M5: Any,
    W_M4: Any,
    L_M4: Any,
    W_M3: Any,
    L_M3: Any,
    C0: Any,
) -> Circuit:
    circuit = Circuit("AnalogGenie 754 / circuit_0609")

    # VSS is treated as the PySpice ground node. Other source values
    # are caller-supplied variables because the dataset has no values.
    circuit.V("VDD", "VDD", circuit.gnd, VDD)
    circuit.V("VIN1", "VIN1", circuit.gnd, VIN1)
    circuit.V("VB1", "VB1", circuit.gnd, VB1)
    circuit.V("VB2", "VB2", circuit.gnd, VB2)
    circuit.V("VCLK1", "VCLK1", circuit.gnd, VCLK1)
    circuit.V("VCLK2", "VCLK2", circuit.gnd, VCLK2)

    # Model names, W/L dimensions, and passive values are variables.
    circuit.raw_spice += "Q11 net15 net15 net53 0 " + str(PNP_MODEL) + "\n"
    circuit.raw_spice += "Q10 net15 net15 net52 0 " + str(PNP_MODEL) + "\n"
    circuit.raw_spice += "Q1 net12 VB1 net50 0 " + str(PNP_MODEL) + "\n"
    circuit.raw_spice += "Q0 net18 VB1 net49 0 " + str(PNP_MODEL) + "\n"
    circuit.raw_spice += "Q15 net26 VB2 net42 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q14 net32 net25 net28 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q13 net28 net28 net44 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q12 net25 net28 net43 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q9 net20 VB2 net45 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q8 net15 VB2 net46 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q7 net8 VB2 net47 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q6 VDD net7 net8 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q5 VDD VIN1 net20 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q4 net18 net20 net51 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q3 net12 net8 net54 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q2 VDD VB1 net48 0 " + str(NPN_MODEL) + "\n"
    circuit.R("15", "VOUT1", circuit.gnd, R15)
    circuit.R("14", "net42", circuit.gnd, R14)
    circuit.R("13", "VDD", "net49", R13)
    circuit.R("12", "VDD", "net50", R12)
    circuit.R("11", "net40", circuit.gnd, R11)
    circuit.R("10", "net40", circuit.gnd, R10)
    circuit.R("9", "net24", circuit.gnd, R9)
    circuit.R("8", "net24", circuit.gnd, R8)
    circuit.R("7", "net10", circuit.gnd, R7)
    circuit.R("6", "net44", "net40", R6)
    circuit.R("5", "net43", "net24", R5)
    circuit.R("4", "net45", "net10", R4)
    circuit.R("3", "net46", "net10", R3)
    circuit.R("2", "net47", "net10", R2)
    circuit.R("1", "net51", "net52", R1)
    circuit.R("0", "net54", "net53", R0)
    circuit.MOSFET("2", "net26", "net26", "net48", "VDD", model=PMOS_MODEL, width=W_M2, length=L_M2)
    circuit.MOSFET("1", "net32", "net26", "net12", "VDD", model=PMOS_MODEL, width=W_M1, length=L_M1)
    circuit.MOSFET("0", "net25", "net26", "net18", "VDD", model=PMOS_MODEL, width=W_M0, length=L_M0)
    circuit.MOSFET("6", "VOUT1", "VCLK1", "net7", circuit.gnd, model=NMOS_MODEL, width=W_M6, length=L_M6)
    circuit.MOSFET("5", "VDD", "net33", "VOUT1", circuit.gnd, model=NMOS_MODEL, width=W_M5, length=L_M5)
    circuit.MOSFET("4", "net33", "VCLK1", "net32", circuit.gnd, model=NMOS_MODEL, width=W_M4, length=L_M4)
    circuit.MOSFET("3", "net32", "VCLK2", "net7", circuit.gnd, model=NMOS_MODEL, width=W_M3, length=L_M3)
    circuit.C("0", "net33", circuit.gnd, C0)
    return circuit
