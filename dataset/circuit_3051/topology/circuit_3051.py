"""Generated from circuit_3051.cir by cadence_to_pyspice.py.

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
    VCLK1: Any,
    VCLK2: Any,
    NMOS_MODEL: Any,
    PMOS_MODEL: Any,
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
    W_MM8_dup2: Any,
    L_MM8_dup2: Any,
    W_MM10: Any,
    L_MM10: Any,
    W_MM2_dup2: Any,
    L_MM2_dup2: Any,
    W_MM0_dup2: Any,
    L_MM0_dup2: Any,
    W_MM3_dup2: Any,
    L_MM3_dup2: Any,
    W_MM1_dup2: Any,
    L_MM1_dup2: Any,
    W_M14: Any,
    L_M14: Any,
    W_M15: Any,
    L_M15: Any,
    W_M13: Any,
    L_M13: Any,
    W_M12: Any,
    L_M12: Any,
    C1: Any,
    C0: Any,
) -> Circuit:
    circuit = Circuit("AnalogGenie 3212 / circuit_3051")

    # VSS is treated as the PySpice ground node. Other source values
    # are caller-supplied variables because the dataset has no values.
    circuit.V("VDD", "VDD", circuit.gnd, VDD)
    circuit.V("VIN1", "VIN1", circuit.gnd, VIN1)
    circuit.V("VB1", "VB1", circuit.gnd, VB1)
    circuit.V("VCLK1", "VCLK1", circuit.gnd, VCLK1)
    circuit.V("VCLK2", "VCLK2", circuit.gnd, VCLK2)

    # Model names, W/L dimensions, and passive values are variables.
    circuit.MOSFET("M8", "net9", "net9", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_MM8, length=L_MM8)
    circuit.MOSFET("M7", "VOUT1", "net9", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_MM7, length=L_MM7)
    circuit.MOSFET("M3", "net20", circuit.gnd, "net16", circuit.gnd, model=NMOS_MODEL, width=W_MM3, length=L_MM3)
    circuit.MOSFET("M0", "net11", "net23", "net16", circuit.gnd, model=NMOS_MODEL, width=W_MM0, length=L_MM0)
    circuit.MOSFET("M4", "net16", "net1005", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_MM4, length=L_MM4)
    circuit.MOSFET("M6", "net9", "net11", "VDD", "VDD", model=PMOS_MODEL, width=W_MM6, length=L_MM6)
    circuit.MOSFET("M5", "VOUT1", "net20", "VDD", "VDD", model=PMOS_MODEL, width=W_MM5, length=L_MM5)
    circuit.MOSFET("M1", "net20", "net20", "VDD", "VDD", model=PMOS_MODEL, width=W_MM1, length=L_MM1)
    circuit.MOSFET("M2", "net11", "net11", "VDD", "VDD", model=PMOS_MODEL, width=W_MM2, length=L_MM2)
    circuit.MOSFET("M8_dup2", "net1005", "net1005", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_MM8_dup2, length=L_MM8_dup2)
    circuit.MOSFET("M10", "net1005", "net1006", "VDD", "VDD", model=PMOS_MODEL, width=W_MM10, length=L_MM10)
    circuit.MOSFET("M2_dup2", "net1006", "VB1", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_MM2_dup2, length=L_MM2_dup2)
    circuit.MOSFET("M0_dup2", "VB1", "VB1", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_MM0_dup2, length=L_MM0_dup2)
    circuit.MOSFET("M3_dup2", "net1006", "net1006", "VDD", "VDD", model=PMOS_MODEL, width=W_MM3_dup2, length=L_MM3_dup2)
    circuit.MOSFET("M1_dup2", "VB1", "net1006", "VDD", "VDD", model=PMOS_MODEL, width=W_MM1_dup2, length=L_MM1_dup2)
    circuit.MOSFET("14", "VIN1", "VCLK1", "net21", circuit.gnd, model=NMOS_MODEL, width=W_M14, length=L_M14)
    circuit.MOSFET("15", "net22", "VCLK2", "net23", circuit.gnd, model=NMOS_MODEL, width=W_M15, length=L_M15)
    circuit.MOSFET("13", "net22", "VCLK1", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M13, length=L_M13)
    circuit.MOSFET("12", "net21", "VCLK2", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M12, length=L_M12)
    circuit.C("1", "net23", "VOUT1", C1)
    circuit.C("0", "net22", "net21", C0)
    return circuit
