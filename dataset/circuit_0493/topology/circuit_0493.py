"""Generated from circuit_0493.cir by cadence_to_pyspice.py.

All values absent from the source netlist remain required variables.
"""

from __future__ import annotations

from typing import Any

from PySpice.Spice.Netlist import Circuit


def build_circuit(
    *,
    VDD: Any,
    VIN1: Any,
    VIN2: Any,
    IB1: Any,
    NMOS_MODEL: Any,
    PMOS_MODEL: Any,
    C0: Any,
    C1: Any,
    W_M11: Any,
    L_M11: Any,
    W_M12: Any,
    L_M12: Any,
    W_M10: Any,
    L_M10: Any,
    W_M9: Any,
    L_M9: Any,
    W_M15: Any,
    L_M15: Any,
    W_M16: Any,
    L_M16: Any,
    W_M8: Any,
    L_M8: Any,
    W_M17: Any,
    L_M17: Any,
    W_M22: Any,
    L_M22: Any,
    W_M24: Any,
    L_M24: Any,
    W_M25: Any,
    L_M25: Any,
    W_M27: Any,
    L_M27: Any,
    W_M21: Any,
    L_M21: Any,
    W_M0: Any,
    L_M0: Any,
    W_M1: Any,
    L_M1: Any,
    W_M4: Any,
    L_M4: Any,
    W_M5: Any,
    L_M5: Any,
    W_M2: Any,
    L_M2: Any,
    W_M3: Any,
    L_M3: Any,
    W_M6: Any,
    L_M6: Any,
    W_M7: Any,
    L_M7: Any,
    W_M14: Any,
    L_M14: Any,
    W_M13: Any,
    L_M13: Any,
    W_M18: Any,
    L_M18: Any,
    W_M19: Any,
    L_M19: Any,
    W_M20: Any,
    L_M20: Any,
    W_M23: Any,
    L_M23: Any,
    W_M26: Any,
    L_M26: Any,
    R0: Any,
) -> Circuit:
    circuit = Circuit("AnalogGenie operational amplifiers 630 (renumbered circuit_0493)")

    # VSS is treated as the PySpice ground node. Other source values
    # are caller-supplied variables because the dataset has no values.
    circuit.V("VDD", "VDD", circuit.gnd, VDD)
    circuit.V("VIN1", "VIN1", circuit.gnd, VIN1)
    circuit.V("VIN2", "VIN2", circuit.gnd, VIN2)
    circuit.I("IB1", circuit.gnd, "IB1", IB1)

    # Model names, W/L dimensions, and passive values are variables.
    circuit.C("0", "net0111", "VOUT1", C0)
    circuit.C("1", "VOUT1", "net0112", C1)
    circuit.MOSFET("11", "net097", "IB1", "net0125", circuit.gnd, model=NMOS_MODEL, width=W_M11, length=L_M11)
    circuit.MOSFET("12", "IB1", "IB1", "net0127", circuit.gnd, model=NMOS_MODEL, width=W_M12, length=L_M12)
    circuit.MOSFET("10", "net0127", "IB1", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M10, length=L_M10)
    circuit.MOSFET("9", "net0125", "IB1", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M9, length=L_M9)
    circuit.MOSFET("15", "net0112", "IB1", "net060", circuit.gnd, model=NMOS_MODEL, width=W_M15, length=L_M15)
    circuit.MOSFET("16", "net056", "IB1", "net059", circuit.gnd, model=NMOS_MODEL, width=W_M16, length=L_M16)
    circuit.MOSFET("8", "net060", "IB1", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M8, length=L_M8)
    circuit.MOSFET("17", "net059", "IB1", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M17, length=L_M17)
    circuit.MOSFET("22", "net0111", "net0110", "net0112", circuit.gnd, model=NMOS_MODEL, width=W_M22, length=L_M22)
    circuit.MOSFET("24", "net8", "net8", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M24, length=L_M24)
    circuit.MOSFET("25", "net0110", "net0110", "net8", circuit.gnd, model=NMOS_MODEL, width=W_M25, length=L_M25)
    circuit.MOSFET("27", "VOUT1", "net0112", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M27, length=L_M27)
    circuit.MOSFET("21", "net0106", "IB1", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M21, length=L_M21)
    circuit.MOSFET("0", "net0128", "net097", "VDD", "VDD", model=PMOS_MODEL, width=W_M0, length=L_M0)
    circuit.MOSFET("1", "net0126", "net097", "VDD", "VDD", model=PMOS_MODEL, width=W_M1, length=L_M1)
    circuit.MOSFET("4", "net0124", "net099", "VDD", "VDD", model=PMOS_MODEL, width=W_M4, length=L_M4)
    circuit.MOSFET("5", "net0116", "net099", "VDD", "VDD", model=PMOS_MODEL, width=W_M5, length=L_M5)
    circuit.MOSFET("2", "net097", "net097", "net0128", "VDD", model=PMOS_MODEL, width=W_M2, length=L_M2)
    circuit.MOSFET("3", "net015", "net097", "net0126", "VDD", model=PMOS_MODEL, width=W_M3, length=L_M3)
    circuit.MOSFET("6", "net099", "net056", "net0124", "VDD", model=PMOS_MODEL, width=W_M6, length=L_M6)
    circuit.MOSFET("7", "net0111", "net056", "net0116", "VDD", model=PMOS_MODEL, width=W_M7, length=L_M7)
    circuit.MOSFET("14", "net059", "VIN2", "net015", "VDD", model=PMOS_MODEL, width=W_M14, length=L_M14)
    circuit.MOSFET("13", "net060", "VIN1", "net015", "VDD", model=PMOS_MODEL, width=W_M13, length=L_M13)
    circuit.MOSFET("18", "net0112", "net0106", "net0111", "VDD", model=PMOS_MODEL, width=W_M18, length=L_M18)
    circuit.MOSFET("19", "net0106", "net0106", "net0107", "VDD", model=PMOS_MODEL, width=W_M19, length=L_M19)
    circuit.MOSFET("20", "net0107", "net0107", "VDD", "VDD", model=PMOS_MODEL, width=W_M20, length=L_M20)
    circuit.MOSFET("23", "net0110", "net097", "VDD", "VDD", model=PMOS_MODEL, width=W_M23, length=L_M23)
    circuit.MOSFET("26", "VOUT1", "net0111", "VDD", "VDD", model=PMOS_MODEL, width=W_M26, length=L_M26)
    circuit.R("0", "net099", "net056", R0)
    return circuit
