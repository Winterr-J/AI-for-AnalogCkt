"""Generated from circuit_0618.cir by cadence_to_pyspice.py.

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
    VHOLD1: Any,
    VTRACK1: Any,
    NMOS_MODEL: Any,
    PMOS_MODEL: Any,
    NPN_MODEL: Any,
    DIODE_MODEL: Any,
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
    W_M7: Any,
    L_M7: Any,
    R0: Any,
) -> Circuit:
    circuit = Circuit("AnalogGenie 764 / circuit_0618")

    # VSS is treated as the PySpice ground node. Other source values
    # are caller-supplied variables because the dataset has no values.
    circuit.V("VDD", "VDD", circuit.gnd, VDD)
    circuit.V("VIN1", "VIN1", circuit.gnd, VIN1)
    circuit.V("VB1", "VB1", circuit.gnd, VB1)
    circuit.I("IB1", circuit.gnd, "IB1", IB1)
    circuit.V("VHOLD1", "VHOLD1", circuit.gnd, VHOLD1)
    circuit.V("VTRACK1", "VTRACK1", circuit.gnd, VTRACK1)

    # Model names, W/L dimensions, and passive values are variables.
    circuit.MOSFET("6", "net24", "VB1", "net42", "VDD", model=PMOS_MODEL, width=W_M6, length=L_M6)
    circuit.MOSFET("5", "net34", "VB1", "net43", "VDD", model=PMOS_MODEL, width=W_M5, length=L_M5)
    circuit.MOSFET("4", "net15", "VB1", "net44", "VDD", model=PMOS_MODEL, width=W_M4, length=L_M4)
    circuit.MOSFET("3", "net42", "net16", "VDD", "VDD", model=PMOS_MODEL, width=W_M3, length=L_M3)
    circuit.MOSFET("2", "net43", "net16", "VDD", "VDD", model=PMOS_MODEL, width=W_M2, length=L_M2)
    circuit.MOSFET("1", "net44", "net16", "VDD", "VDD", model=PMOS_MODEL, width=W_M1, length=L_M1)
    circuit.MOSFET("0", "net30", "net16", "VDD", "VDD", model=PMOS_MODEL, width=W_M0, length=L_M0)
    circuit.raw_spice += "Q3 net24 VHOLD1 IB1 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q2 net34 VTRACK1 IB1 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q1 net15 VIN1 net24 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q0 net15 VIN1 net34 0 " + str(NPN_MODEL) + "\n"
    circuit.D("3", "VOUT2", "net24", model=DIODE_MODEL)
    circuit.D("2", "net24", "VOUT1", model=DIODE_MODEL)
    circuit.D("1", "net34", "VOUT2", model=DIODE_MODEL)
    circuit.D("0", "VOUT1", "net34", model=DIODE_MODEL)
    circuit.C("1", "VOUT2", circuit.gnd, C1)
    circuit.C("0", "VOUT1", circuit.gnd, C0)
    circuit.MOSFET("7", "VDD", "net15", "net30", circuit.gnd, model=NMOS_MODEL, width=W_M7, length=L_M7)
    circuit.R("0", "net30", circuit.gnd, R0)
    return circuit
