"""Generated from circuit_1844.cir by cadence_to_pyspice.py.

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
    NMOS_MODEL: Any,
    PMOS_MODEL: Any,
    W_MM7: Any,
    L_MM7: Any,
    W_MM6: Any,
    L_MM6: Any,
    W_MM5: Any,
    L_MM5: Any,
    W_MM1: Any,
    L_MM1: Any,
    W_MM0: Any,
    L_MM0: Any,
    W_MM9: Any,
    L_MM9: Any,
    W_MM8: Any,
    L_MM8: Any,
    W_MM10: Any,
    L_MM10: Any,
    W_MM3: Any,
    L_MM3: Any,
    W_MM0_dup2: Any,
    L_MM0_dup2: Any,
    W_MM1_dup2: Any,
    L_MM1_dup2: Any,
    W_MM2: Any,
    L_MM2: Any,
    RRF: Any,
    RR0: Any,
) -> Circuit:
    circuit = Circuit("AnalogGenie 2004 / circuit_1844")

    # VSS is treated as the PySpice ground node. Other source values
    # are caller-supplied variables because the dataset has no values.
    circuit.V("VDD", "VDD", circuit.gnd, VDD)
    circuit.V("VIN1", "VIN1", circuit.gnd, VIN1)
    circuit.V("VIN2", "VIN2", circuit.gnd, VIN2)

    # Model names, W/L dimensions, and passive values are variables.
    circuit.MOSFET("M7", "VOUT1", "VIN2", "net11", "net18", model=PMOS_MODEL, width=W_MM7, length=L_MM7)
    circuit.MOSFET("M6", "net13", "VIN1", "net11", "net18", model=PMOS_MODEL, width=W_MM6, length=L_MM6)
    circuit.MOSFET("M5", "net11", "net1004", "VDD", "VDD", model=PMOS_MODEL, width=W_MM5, length=L_MM5)
    circuit.MOSFET("M1", "VOUT1", "net17", "net20", circuit.gnd, model=NMOS_MODEL, width=W_MM1, length=L_MM1)
    circuit.MOSFET("M0", "net13", "net17", "net19", circuit.gnd, model=NMOS_MODEL, width=W_MM0, length=L_MM0)
    circuit.MOSFET("M9", "net20", "net13", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_MM9, length=L_MM9)
    circuit.MOSFET("M8", "net19", "net13", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_MM8, length=L_MM8)
    circuit.MOSFET("M10", "net1004", "net1007", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_MM10, length=L_MM10)
    circuit.MOSFET("M3", "net1004", "net1004", "VDD", "VDD", model=PMOS_MODEL, width=W_MM3, length=L_MM3)
    circuit.MOSFET("M0_dup2", "net1007", "net1007", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_MM0_dup2, length=L_MM0_dup2)
    circuit.MOSFET("M1_dup2", "net10", "net1007", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_MM1_dup2, length=L_MM1_dup2)
    circuit.MOSFET("M2", "net1007", "net10", "VDD", "VDD", model=PMOS_MODEL, width=W_MM2, length=L_MM2)
    circuit.R("RF", "VDD", "net1007", RRF)
    circuit.R("R0", "VDD", "net10", RR0)
    return circuit
