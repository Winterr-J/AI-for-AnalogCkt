"""Generated from circuit_0608.cir by cadence_to_pyspice.py.

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
    NMOS_MODEL: Any,
    PMOS_MODEL: Any,
    NPN_MODEL: Any,
    PNP_MODEL: Any,
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
    W_M5: Any,
    L_M5: Any,
    W_M4: Any,
    L_M4: Any,
    C0: Any,
) -> Circuit:
    circuit = Circuit("AnalogGenie 753 / circuit_0608")

    # VSS is treated as the PySpice ground node. Other source values
    # are caller-supplied variables because the dataset has no values.
    circuit.V("VDD", "VDD", circuit.gnd, VDD)
    circuit.V("VIN1", "VIN1", circuit.gnd, VIN1)
    circuit.V("VB1", "VB1", circuit.gnd, VB1)
    circuit.V("VB2", "VB2", circuit.gnd, VB2)
    circuit.V("VCLK1", "VCLK1", circuit.gnd, VCLK1)

    # Model names, W/L dimensions, and passive values are variables.
    circuit.raw_spice += "Q13 net23 VB2 VDD 0 " + str(PNP_MODEL) + "\n"
    circuit.raw_spice += "Q7 net7 net7 net29 0 " + str(PNP_MODEL) + "\n"
    circuit.raw_spice += "Q6 net8 VB2 net31 0 " + str(PNP_MODEL) + "\n"
    circuit.raw_spice += "Q2 net7 net7 net32 0 " + str(PNP_MODEL) + "\n"
    circuit.raw_spice += "Q0 net3 VB2 net34 0 " + str(PNP_MODEL) + "\n"
    circuit.R("11", "VOUT1", circuit.gnd, R11)
    circuit.R("10", "net22", circuit.gnd, R10)
    circuit.R("9", "net24", circuit.gnd, R9)
    circuit.R("8", "net5", circuit.gnd, R8)
    circuit.R("7", "net25", circuit.gnd, R7)
    circuit.R("6", "net27", "net5", R6)
    circuit.R("5", "net28", "net5", R5)
    circuit.R("4", "net30", "net29", R4)
    circuit.R("3", "VDD", "net31", R3)
    circuit.R("2", "net35", "net5", R2)
    circuit.R("1", "net33", "net32", R1)
    circuit.R("0", "VDD", "net34", R0)
    circuit.raw_spice += "Q15 net21 net11 net13 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q14 net13 net13 net22 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q12 net11 net13 net24 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q11 net12 VB1 net25 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q10 net7 VB1 net27 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q9 net18 VB1 net28 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q8 VDD VIN1 net18 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q5 net8 net18 net30 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q4 net16 VB1 net35 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q3 VDD VOUT1 net16 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q1 net3 net16 net33 0 " + str(NPN_MODEL) + "\n"
    circuit.MOSFET("2", "net21", "net12", "net3", "VDD", model=PMOS_MODEL, width=W_M2, length=L_M2)
    circuit.MOSFET("1", "net12", "net12", "net23", "VDD", model=PMOS_MODEL, width=W_M1, length=L_M1)
    circuit.MOSFET("0", "net11", "net12", "net8", "VDD", model=PMOS_MODEL, width=W_M0, length=L_M0)
    circuit.MOSFET("5", "VDD", "net015", "VOUT1", circuit.gnd, model=NMOS_MODEL, width=W_M5, length=L_M5)
    circuit.MOSFET("4", "net015", "VCLK1", "net21", circuit.gnd, model=NMOS_MODEL, width=W_M4, length=L_M4)
    circuit.C("0", "net015", circuit.gnd, C0)
    return circuit
