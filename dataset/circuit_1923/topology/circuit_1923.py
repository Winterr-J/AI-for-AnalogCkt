"""Generated from circuit_1923.cir by cadence_to_pyspice.py.

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
    NMOS_MODEL: Any,
    PMOS_MODEL: Any,
    W_MM13: Any,
    L_MM13: Any,
    W_MM9: Any,
    L_MM9: Any,
    W_MM8: Any,
    L_MM8: Any,
    W_MM12: Any,
    L_MM12: Any,
    W_MM11: Any,
    L_MM11: Any,
    W_MM10: Any,
    L_MM10: Any,
    W_MM7: Any,
    L_MM7: Any,
    CC2: Any,
    W_MM4: Any,
    L_MM4: Any,
    W_MM2: Any,
    L_MM2: Any,
    W_MM10_dup2: Any,
    L_MM10_dup2: Any,
    W_MM3: Any,
    L_MM3: Any,
    W_MM1: Any,
    L_MM1: Any,
    W_MM0: Any,
    L_MM0: Any,
    W_MM0_dup2: Any,
    L_MM0_dup2: Any,
    W_MM1_dup2: Any,
    L_MM1_dup2: Any,
    W_MM2_dup2: Any,
    L_MM2_dup2: Any,
    RRF: Any,
    RR0: Any,
) -> Circuit:
    circuit = Circuit("AnalogGenie 2083 / circuit_1923")

    # VSS is treated as the PySpice ground node. Other source values
    # are caller-supplied variables because the dataset has no values.
    circuit.V("VDD", "VDD", circuit.gnd, VDD)
    circuit.V("VIN1", "VIN1", circuit.gnd, VIN1)
    circuit.V("VIN2", "VIN2", circuit.gnd, VIN2)
    circuit.V("VB1", "VB1", circuit.gnd, VB1)

    # Model names, W/L dimensions, and passive values are variables.
    circuit.MOSFET("M13", "VOUT1", "net38", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_MM13, length=L_MM13)
    circuit.MOSFET("M9", "net38", "net35", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_MM9, length=L_MM9)
    circuit.MOSFET("M8", "net35", "net35", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_MM8, length=L_MM8)
    circuit.MOSFET("M12", "VOUT1", "net1004", "VDD", "VDD", model=PMOS_MODEL, width=W_MM12, length=L_MM12)
    circuit.MOSFET("M11", "net33", "net1004", "VDD", "VDD", model=PMOS_MODEL, width=W_MM11, length=L_MM11)
    circuit.MOSFET("M10", "net38", "VIN2", "net33", "net39", model=PMOS_MODEL, width=W_MM10, length=L_MM10)
    circuit.MOSFET("M7", "net35", "VIN1", "net33", "net39", model=PMOS_MODEL, width=W_MM7, length=L_MM7)
    circuit.C("C2", "VOUT1", "net38", CC2)
    circuit.MOSFET("M4", "net1004", "VB1", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_MM4, length=L_MM4)
    circuit.MOSFET("M2", "VB1", "VB1", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_MM2, length=L_MM2)
    circuit.MOSFET("M10_dup2", "neta", "net1007", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_MM10_dup2, length=L_MM10_dup2)
    circuit.MOSFET("M3", "net1004", "net1004", "VDD", "VDD", model=PMOS_MODEL, width=W_MM3, length=L_MM3)
    circuit.MOSFET("M1", "VB1", "neta", "VDD", "VDD", model=PMOS_MODEL, width=W_MM1, length=L_MM1)
    circuit.MOSFET("M0", "neta", "neta", "VDD", "VDD", model=PMOS_MODEL, width=W_MM0, length=L_MM0)
    circuit.MOSFET("M0_dup2", "net1007", "net1007", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_MM0_dup2, length=L_MM0_dup2)
    circuit.MOSFET("M1_dup2", "net10", "net1007", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_MM1_dup2, length=L_MM1_dup2)
    circuit.MOSFET("M2_dup2", "net1007", "net10", "VDD", "VDD", model=PMOS_MODEL, width=W_MM2_dup2, length=L_MM2_dup2)
    circuit.R("RF", "VDD", "net1007", RRF)
    circuit.R("R0", "VDD", "net10", RR0)
    return circuit
