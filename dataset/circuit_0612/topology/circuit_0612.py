"""Generated from circuit_0612.cir by cadence_to_pyspice.py.

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
    IB1: Any,
    IB2: Any,
    IB3: Any,
    VCLK1: Any,
    NMOS_MODEL: Any,
    PMOS_MODEL: Any,
    NPN_MODEL: Any,
    W_M2: Any,
    L_M2: Any,
    W_M1: Any,
    L_M1: Any,
    W_M0: Any,
    L_M0: Any,
    C2: Any,
    C1: Any,
    C0: Any,
    R2: Any,
    R1: Any,
    R0: Any,
    W_M5: Any,
    L_M5: Any,
    W_M3: Any,
    L_M3: Any,
) -> Circuit:
    circuit = Circuit("AnalogGenie 758 / circuit_0612")

    # VSS is treated as the PySpice ground node. Other source values
    # are caller-supplied variables because the dataset has no values.
    circuit.V("VDD", "VDD", circuit.gnd, VDD)
    circuit.V("VIN1", "VIN1", circuit.gnd, VIN1)
    circuit.V("VB1", "VB1", circuit.gnd, VB1)
    circuit.I("IB1", circuit.gnd, "IB1", IB1)
    circuit.I("IB2", circuit.gnd, "IB2", IB2)
    circuit.I("IB3", circuit.gnd, "IB3", IB3)
    circuit.V("VCLK1", "VCLK1", circuit.gnd, VCLK1)

    # Model names, W/L dimensions, and passive values are variables.
    circuit.MOSFET("2", "net15", "net12", "VDD", "VDD", model=PMOS_MODEL, width=W_M2, length=L_M2)
    circuit.MOSFET("1", "IB3", "IB3", "VDD", "VDD", model=PMOS_MODEL, width=W_M1, length=L_M1)
    circuit.MOSFET("0", "net12", "IB3", "VDD", "VDD", model=PMOS_MODEL, width=W_M0, length=L_M0)
    circuit.raw_spice += "Q6 VDD net15 VOUT1 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q5 net32 IB2 net14 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q4 net15 VB1 net32 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q3 net31 IB1 net14 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q2 net12 VB1 net31 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q1 VDD net9 IB2 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q0 VDD net8 IB1 0 " + str(NPN_MODEL) + "\n"
    circuit.C("2", "net9", "VOUT1", C2)
    circuit.C("1", "net8", circuit.gnd, C1)
    circuit.C("0", "net12", "net33", C0)
    circuit.R("2", "VOUT1", circuit.gnd, R2)
    circuit.R("1", "net14", circuit.gnd, R1)
    circuit.R("0", "net33", "net15", R0)
    circuit.MOSFET("5", "net9", "VCLK1", "VOUT1", circuit.gnd, model=NMOS_MODEL, width=W_M5, length=L_M5)
    circuit.MOSFET("3", "net8", "VCLK1", "VIN1", circuit.gnd, model=NMOS_MODEL, width=W_M3, length=L_M3)
    return circuit
