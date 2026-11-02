"""Generated from circuit_2014.cir by cadence_to_pyspice.py.

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
    W_MM0_dup2: Any,
    L_MM0_dup2: Any,
    W_MM1_dup2: Any,
    L_MM1_dup2: Any,
    W_MM3_dup2: Any,
    L_MM3_dup2: Any,
    RR1: Any,
    RR0: Any,
    RRF: Any,
    W_MM1_dup3: Any,
    L_MM1_dup3: Any,
    W_MM0_dup3: Any,
    L_MM0_dup3: Any,
) -> Circuit:
    circuit = Circuit("AnalogGenie 2174 / circuit_2014")

    # VSS is treated as the PySpice ground node. Other source values
    # are caller-supplied variables because the dataset has no values.
    circuit.V("VDD", "VDD", circuit.gnd, VDD)
    circuit.V("VIN1", "VIN1", circuit.gnd, VIN1)
    circuit.V("VIN2", "VIN2", circuit.gnd, VIN2)
    circuit.V("VB1", "VB1", circuit.gnd, VB1)
    circuit.V("VB2", "VB2", circuit.gnd, VB2)

    # Model names, W/L dimensions, and passive values are variables.
    circuit.MOSFET("M3", "VOUT1", "VIN1", "net11", circuit.gnd, model=NMOS_MODEL, width=W_MM3, length=L_MM3)
    circuit.MOSFET("M0", "net13", "VIN2", "net11", circuit.gnd, model=NMOS_MODEL, width=W_MM0, length=L_MM0)
    circuit.MOSFET("M4", "net11", "VB1", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_MM4, length=L_MM4)
    circuit.MOSFET("M6", "net13", "net1002", "net016", "VDD", model=PMOS_MODEL, width=W_MM6, length=L_MM6)
    circuit.MOSFET("M5", "VOUT1", "net1002", "net014", "VDD", model=PMOS_MODEL, width=W_MM5, length=L_MM5)
    circuit.MOSFET("M1", "net014", "net13", "VDD", "VDD", model=PMOS_MODEL, width=W_MM1, length=L_MM1)
    circuit.MOSFET("M2", "net016", "net13", "VDD", "VDD", model=PMOS_MODEL, width=W_MM2, length=L_MM2)
    circuit.MOSFET("M0_dup2", "net1002", "net1007", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_MM0_dup2, length=L_MM0_dup2)
    circuit.MOSFET("M1_dup2", "net1002", "net1002", "VB2", "VDD", model=PMOS_MODEL, width=W_MM1_dup2, length=L_MM1_dup2)
    circuit.MOSFET("M3_dup2", "VB2", "VB2", "VDD", "VDD", model=PMOS_MODEL, width=W_MM3_dup2, length=L_MM3_dup2)
    circuit.R("R1", "net1007", "net7", RR1)
    circuit.R("R0", "VDD", "net7", RR0)
    circuit.R("RF", "VDD", "net1007", RRF)
    circuit.MOSFET("M1_dup3", "net7", "net1007", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_MM1_dup3, length=L_MM1_dup3)
    circuit.MOSFET("M0_dup3", "net1007", "net1007", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_MM0_dup3, length=L_MM0_dup3)
    return circuit
