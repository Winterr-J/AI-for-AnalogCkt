"""Generated from circuit_1772.cir by cadence_to_pyspice.py.

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
    W_MM3: Any,
    L_MM3: Any,
    W_MM0: Any,
    L_MM0: Any,
    W_MM4: Any,
    L_MM4: Any,
    W_MM1: Any,
    L_MM1: Any,
    W_MM2: Any,
    L_MM2: Any,
    W_MM1_dup2: Any,
    L_MM1_dup2: Any,
    W_MM0_dup2: Any,
    L_MM0_dup2: Any,
    W_MM8: Any,
    L_MM8: Any,
    W_MM2_dup2: Any,
    L_MM2_dup2: Any,
    W_MM3_dup2: Any,
    L_MM3_dup2: Any,
    W_MM10: Any,
    L_MM10: Any,
    RR1: Any,
    W_MM2_dup3: Any,
    L_MM2_dup3: Any,
) -> Circuit:
    circuit = Circuit("AnalogGenie 1932 / circuit_1772")

    # VSS is treated as the PySpice ground node. Other source values
    # are caller-supplied variables because the dataset has no values.
    circuit.V("VDD", "VDD", circuit.gnd, VDD)
    circuit.V("VIN1", "VIN1", circuit.gnd, VIN1)
    circuit.V("VIN2", "VIN2", circuit.gnd, VIN2)

    # Model names, W/L dimensions, and passive values are variables.
    circuit.MOSFET("M3", "VOUT1", "VIN1", "net16", circuit.gnd, model=NMOS_MODEL, width=W_MM3, length=L_MM3)
    circuit.MOSFET("M0", "net1004", "VIN2", "net16", circuit.gnd, model=NMOS_MODEL, width=W_MM0, length=L_MM0)
    circuit.MOSFET("M4", "net16", "net1005", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_MM4, length=L_MM4)
    circuit.MOSFET("M1", "VOUT1", "net1004", "VDD", "VDD", model=PMOS_MODEL, width=W_MM1, length=L_MM1)
    circuit.MOSFET("M2", "net1004", "net1004", "VDD", "VDD", model=PMOS_MODEL, width=W_MM2, length=L_MM2)
    circuit.MOSFET("M1_dup2", "net1005", "net1005", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_MM1_dup2, length=L_MM1_dup2)
    circuit.MOSFET("M0_dup2", "net1004", "net6", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_MM0_dup2, length=L_MM0_dup2)
    circuit.MOSFET("M8", "net6", "net6", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_MM8, length=L_MM8)
    circuit.MOSFET("M2_dup2", "net1005", "net1004", "VDD", "VDD", model=PMOS_MODEL, width=W_MM2_dup2, length=L_MM2_dup2)
    circuit.MOSFET("M3_dup2", "net1004", "net1004", "VDD", "VDD", model=PMOS_MODEL, width=W_MM3_dup2, length=L_MM3_dup2)
    circuit.MOSFET("M10", "net6", "net1006", "VDD", "VDD", model=PMOS_MODEL, width=W_MM10, length=L_MM10)
    circuit.R("R1", "net1006", circuit.gnd, RR1)
    circuit.MOSFET("M2_dup3", "net1006", "net1006", "VDD", "VDD", model=PMOS_MODEL, width=W_MM2_dup3, length=L_MM2_dup3)
    return circuit
