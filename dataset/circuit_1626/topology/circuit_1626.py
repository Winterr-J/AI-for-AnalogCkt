"""Generated from circuit_1626.cir by cadence_to_pyspice.py.

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
    VB1: Any,
    VB2: Any,
    NMOS_MODEL: Any,
    PMOS_MODEL: Any,
    W_MM10: Any,
    L_MM10: Any,
    W_MM9: Any,
    L_MM9: Any,
    W_MM8: Any,
    L_MM8: Any,
    W_MM7: Any,
    L_MM7: Any,
    W_MM3: Any,
    L_MM3: Any,
    W_MM0: Any,
    L_MM0: Any,
    W_MM4: Any,
    L_MM4: Any,
    W_MM6: Any,
    L_MM6: Any,
    W_MM5: Any,
    L_MM5: Any,
    W_MM1: Any,
    L_MM1: Any,
    W_MM2: Any,
    L_MM2: Any,
    W_MM15: Any,
    L_MM15: Any,
    W_MM13: Any,
    L_MM13: Any,
    W_MM14: Any,
    L_MM14: Any,
    RR0: Any,
    RR1: Any,
    W_MM2_dup2: Any,
    L_MM2_dup2: Any,
) -> Circuit:
    circuit = Circuit("AnalogGenie 1786 / circuit_1626")

    # VSS is treated as the PySpice ground node. Other source values
    # are caller-supplied variables because the dataset has no values.
    circuit.V("VDD", "VDD", circuit.gnd, VDD)
    circuit.V("VIN1", "VIN1", circuit.gnd, VIN1)
    circuit.V("VIN2", "VIN2", circuit.gnd, VIN2)
    circuit.V("VB1", "VB1", circuit.gnd, VB1)
    circuit.V("VB2", "VB2", circuit.gnd, VB2)

    # Model names, W/L dimensions, and passive values are variables.
    circuit.MOSFET("M10", "net27", "net12", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_MM10, length=L_MM10)
    circuit.MOSFET("M9", "net21", "net12", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_MM9, length=L_MM9)
    circuit.MOSFET("M8", "net12", "net1003", "net27", circuit.gnd, model=NMOS_MODEL, width=W_MM8, length=L_MM8)
    circuit.MOSFET("M7", "VOUT1", "net1003", "net21", circuit.gnd, model=NMOS_MODEL, width=W_MM7, length=L_MM7)
    circuit.MOSFET("M3", "net32", "VIN1", "net10", circuit.gnd, model=NMOS_MODEL, width=W_MM3, length=L_MM3)
    circuit.MOSFET("M0", "net26", "VIN2", "net10", circuit.gnd, model=NMOS_MODEL, width=W_MM0, length=L_MM0)
    circuit.MOSFET("M4", "net10", "net1001", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_MM4, length=L_MM4)
    circuit.MOSFET("M6", "net12", "VB2", "net26", "VDD", model=PMOS_MODEL, width=W_MM6, length=L_MM6)
    circuit.MOSFET("M5", "VOUT1", "VB2", "net32", "VDD", model=PMOS_MODEL, width=W_MM5, length=L_MM5)
    circuit.MOSFET("M1", "net32", "VB1", "VDD", "VDD", model=PMOS_MODEL, width=W_MM1, length=L_MM1)
    circuit.MOSFET("M2", "net26", "VB1", "VDD", "VDD", model=PMOS_MODEL, width=W_MM2, length=L_MM2)
    circuit.MOSFET("M15", "net1003", "net1003", "net1001", circuit.gnd, model=NMOS_MODEL, width=W_MM15, length=L_MM15)
    circuit.MOSFET("M13", "net1001", "net1001", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_MM13, length=L_MM13)
    circuit.MOSFET("M14", "net1003", "net1006", "VDD", "VDD", model=PMOS_MODEL, width=W_MM14, length=L_MM14)
    circuit.R("R0", "VDD", "net6", RR0)
    circuit.R("R1", "net1006", circuit.gnd, RR1)
    circuit.MOSFET("M2_dup2", "net1006", "net1006", "net6", "VDD", model=PMOS_MODEL, width=W_MM2_dup2, length=L_MM2_dup2)
    return circuit
