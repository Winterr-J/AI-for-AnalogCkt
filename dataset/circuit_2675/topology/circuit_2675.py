"""Generated from circuit_2675.cir by cadence_to_pyspice.py.

All values absent from the source netlist remain required variables.
"""

from __future__ import annotations

from typing import Any

from PySpice.Spice.Netlist import Circuit


def build_circuit(
    *,
    VDD: Any,
    VIN1: Any,
    VCLK1: Any,
    VCLK2: Any,
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
    W_MM8: Any,
    L_MM8: Any,
    W_MM10: Any,
    L_MM10: Any,
    RR0: Any,
    RR1: Any,
    W_MM2_dup2: Any,
    L_MM2_dup2: Any,
    W_M12: Any,
    L_M12: Any,
    W_M13: Any,
    L_M13: Any,
    C1: Any,
    C0: Any,
) -> Circuit:
    circuit = Circuit("AnalogGenie 2835 / circuit_2675")

    # VSS is treated as the PySpice ground node. Other source values
    # are caller-supplied variables because the dataset has no values.
    circuit.V("VDD", "VDD", circuit.gnd, VDD)
    circuit.V("VIN1", "VIN1", circuit.gnd, VIN1)
    circuit.V("VCLK1", "VCLK1", circuit.gnd, VCLK1)
    circuit.V("VCLK2", "VCLK2", circuit.gnd, VCLK2)

    # Model names, W/L dimensions, and passive values are variables.
    circuit.MOSFET("M3", "VOUT1", circuit.gnd, "net13", circuit.gnd, model=NMOS_MODEL, width=W_MM3, length=L_MM3)
    circuit.MOSFET("M0", "net11", "net12", "net13", circuit.gnd, model=NMOS_MODEL, width=W_MM0, length=L_MM0)
    circuit.MOSFET("M4", "net13", "net1005", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_MM4, length=L_MM4)
    circuit.MOSFET("M6", "net11", "net18", "net19", "VDD", model=PMOS_MODEL, width=W_MM6, length=L_MM6)
    circuit.MOSFET("M5", "VOUT1", "net18", "net20", "VDD", model=PMOS_MODEL, width=W_MM5, length=L_MM5)
    circuit.MOSFET("M1", "net20", "net11", "VDD", "VDD", model=PMOS_MODEL, width=W_MM1, length=L_MM1)
    circuit.MOSFET("M2", "net19", "net11", "VDD", "VDD", model=PMOS_MODEL, width=W_MM2, length=L_MM2)
    circuit.MOSFET("M8", "net1005", "net1005", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_MM8, length=L_MM8)
    circuit.MOSFET("M10", "net1005", "net1006", "VDD", "VDD", model=PMOS_MODEL, width=W_MM10, length=L_MM10)
    circuit.R("R0", "VDD", "net6", RR0)
    circuit.R("R1", "net1006", circuit.gnd, RR1)
    circuit.MOSFET("M2_dup2", "net1006", "net1006", "net6", "VDD", model=PMOS_MODEL, width=W_MM2_dup2, length=L_MM2_dup2)
    circuit.MOSFET("12", "VIN1", "VCLK1", "net11", circuit.gnd, model=NMOS_MODEL, width=W_M12, length=L_M12)
    circuit.MOSFET("13", "net11", "VCLK2", "net12", circuit.gnd, model=NMOS_MODEL, width=W_M13, length=L_M13)
    circuit.C("1", "net12", "VOUT1", C1)
    circuit.C("0", "net11", circuit.gnd, C0)
    return circuit
