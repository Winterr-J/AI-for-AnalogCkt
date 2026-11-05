"""Generated from circuit_0615.cir by cadence_to_pyspice.py.

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
    VB4: Any,
    VB5: Any,
    VB6: Any,
    IB1: Any,
    IB2: Any,
    VCLK1: Any,
    VCLK2: Any,
    VCLK3: Any,
    VCLK4: Any,
    VCLK5: Any,
    NMOS_MODEL: Any,
    PMOS_MODEL: Any,
    W_M19: Any,
    L_M19: Any,
    W_M12: Any,
    L_M12: Any,
    W_M11: Any,
    L_M11: Any,
    W_M10: Any,
    L_M10: Any,
    W_M3: Any,
    L_M3: Any,
    W_M2: Any,
    L_M2: Any,
    W_M1: Any,
    L_M1: Any,
    W_M0: Any,
    L_M0: Any,
    W_M27: Any,
    L_M27: Any,
    W_M26: Any,
    L_M26: Any,
    W_M25: Any,
    L_M25: Any,
    W_M24: Any,
    L_M24: Any,
    W_M23: Any,
    L_M23: Any,
    W_M22: Any,
    L_M22: Any,
    W_M21: Any,
    L_M21: Any,
    W_M20: Any,
    L_M20: Any,
    W_M18: Any,
    L_M18: Any,
    W_M17: Any,
    L_M17: Any,
    W_M16: Any,
    L_M16: Any,
    W_M15: Any,
    L_M15: Any,
    W_M14: Any,
    L_M14: Any,
    W_M13: Any,
    L_M13: Any,
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
    C3: Any,
    C2: Any,
    C1: Any,
    C0: Any,
    R0: Any,
) -> Circuit:
    circuit = Circuit("AnalogGenie 761 / circuit_0615")

    # VSS is treated as the PySpice ground node. Other source values
    # are caller-supplied variables because the dataset has no values.
    circuit.V("VDD", "VDD", circuit.gnd, VDD)
    circuit.V("VIN1", "VIN1", circuit.gnd, VIN1)
    circuit.V("VB1", "VB1", circuit.gnd, VB1)
    circuit.V("VB2", "VB2", circuit.gnd, VB2)
    circuit.V("VB3", "VB3", circuit.gnd, VB3)
    circuit.V("VB4", "VB4", circuit.gnd, VB4)
    circuit.V("VB5", "VB5", circuit.gnd, VB5)
    circuit.V("VB6", "VB6", circuit.gnd, VB6)
    circuit.I("IB1", circuit.gnd, "IB1", IB1)
    circuit.I("IB2", circuit.gnd, "IB2", IB2)
    circuit.V("VCLK1", "VCLK1", circuit.gnd, VCLK1)
    circuit.V("VCLK2", "VCLK2", circuit.gnd, VCLK2)
    circuit.V("VCLK3", "VCLK3", circuit.gnd, VCLK3)
    circuit.V("VCLK4", "VCLK4", circuit.gnd, VCLK4)
    circuit.V("VCLK5", "VCLK5", circuit.gnd, VCLK5)

    # Model names, W/L dimensions, and passive values are variables.
    circuit.MOSFET("19", "net46", "VB6", "VDD", "VDD", model=PMOS_MODEL, width=W_M19, length=L_M19)
    circuit.MOSFET("12", "VOUT1", "VB5", "net26", "VDD", model=PMOS_MODEL, width=W_M12, length=L_M12)
    circuit.MOSFET("11", "net019", "VB5", "net46", "VDD", model=PMOS_MODEL, width=W_M11, length=L_M11)
    circuit.MOSFET("10", "net26", "VB6", "VDD", "VDD", model=PMOS_MODEL, width=W_M10, length=L_M10)
    circuit.MOSFET("3", "net16", "VB2", "net11", "VDD", model=PMOS_MODEL, width=W_M3, length=L_M3)
    circuit.MOSFET("2", "net05", "VB2", "net38", "VDD", model=PMOS_MODEL, width=W_M2, length=L_M2)
    circuit.MOSFET("1", "net11", "VB3", "VDD", "VDD", model=PMOS_MODEL, width=W_M1, length=L_M1)
    circuit.MOSFET("0", "net38", "VB3", "VDD", "VDD", model=PMOS_MODEL, width=W_M0, length=L_M0)
    circuit.MOSFET("27", "VDD", "net29", "net28", circuit.gnd, model=NMOS_MODEL, width=W_M27, length=L_M27)
    circuit.MOSFET("26", "net28", "VCLK2", "net31", circuit.gnd, model=NMOS_MODEL, width=W_M26, length=L_M26)
    circuit.MOSFET("25", "net28", "VCLK5", "net31", circuit.gnd, model=NMOS_MODEL, width=W_M25, length=L_M25)
    circuit.MOSFET("24", "net16", "VCLK2", "net42", circuit.gnd, model=NMOS_MODEL, width=W_M24, length=L_M24)
    circuit.MOSFET("23", "net29", "VCLK4", "net16", circuit.gnd, model=NMOS_MODEL, width=W_M23, length=L_M23)
    circuit.MOSFET("22", "VOUT1", "VCLK3", "net2", circuit.gnd, model=NMOS_MODEL, width=W_M22, length=L_M22)
    circuit.MOSFET("21", "net3", "VCLK2", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M21, length=L_M21)
    circuit.MOSFET("20", "net2", "VCLK1", "VIN1", circuit.gnd, model=NMOS_MODEL, width=W_M20, length=L_M20)
    circuit.MOSFET("18", "net26", "net29", "IB2", circuit.gnd, model=NMOS_MODEL, width=W_M18, length=L_M18)
    circuit.MOSFET("17", "net46", circuit.gnd, "IB2", circuit.gnd, model=NMOS_MODEL, width=W_M17, length=L_M17)
    circuit.MOSFET("16", "net73", "net019", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M16, length=L_M16)
    circuit.MOSFET("15", "net74", "net019", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M15, length=L_M15)
    circuit.MOSFET("14", "VOUT1", "VB4", "net73", circuit.gnd, model=NMOS_MODEL, width=W_M14, length=L_M14)
    circuit.MOSFET("13", "net019", "VB4", "net74", circuit.gnd, model=NMOS_MODEL, width=W_M13, length=L_M13)
    circuit.MOSFET("9", "net11", "net42", "IB1", circuit.gnd, model=NMOS_MODEL, width=W_M9, length=L_M9)
    circuit.MOSFET("8", "net38", "net3", "IB1", circuit.gnd, model=NMOS_MODEL, width=W_M8, length=L_M8)
    circuit.MOSFET("7", "net75", "net05", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M7, length=L_M7)
    circuit.MOSFET("6", "net76", "net05", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M6, length=L_M6)
    circuit.MOSFET("5", "net16", "VB1", "net75", circuit.gnd, model=NMOS_MODEL, width=W_M5, length=L_M5)
    circuit.MOSFET("4", "net05", "VB1", "net76", circuit.gnd, model=NMOS_MODEL, width=W_M4, length=L_M4)
    circuit.C("3", "net31", "net29", C3)
    circuit.C("2", "VOUT1", "net29", C2)
    circuit.C("1", "net42", circuit.gnd, C1)
    circuit.C("0", "net3", "net2", C0)
    circuit.R("0", "net28", circuit.gnd, R0)
    return circuit
