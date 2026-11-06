"""Generated from circuit_0662.cir by cadence_to_pyspice.py.

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
    IB1: Any,
    VCLK1: Any,
    VCLK2: Any,
    NMOS_MODEL: Any,
    PMOS_MODEL: Any,
    W_M11: Any,
    L_M11: Any,
    W_M10: Any,
    L_M10: Any,
    W_M9: Any,
    L_M9: Any,
    W_M8: Any,
    L_M8: Any,
    W_M5: Any,
    L_M5: Any,
    W_M4: Any,
    L_M4: Any,
    W_M7: Any,
    L_M7: Any,
    W_M6: Any,
    L_M6: Any,
    C0: Any,
    C4: Any,
    W_M1: Any,
    L_M1: Any,
    W_M0: Any,
    L_M0: Any,
    W_M3: Any,
    L_M3: Any,
    W_M2: Any,
    L_M2: Any,
) -> Circuit:
    circuit = Circuit("AnalogGenie 810 / circuit_0662")

    # VSS is treated as the PySpice ground node. Other source values
    # are caller-supplied variables because the dataset has no values.
    circuit.V("VDD", "VDD", circuit.gnd, VDD)
    circuit.V("VIN1", "VIN1", circuit.gnd, VIN1)
    circuit.V("VB1", "VB1", circuit.gnd, VB1)
    circuit.V("VB2", "VB2", circuit.gnd, VB2)
    circuit.V("VB3", "VB3", circuit.gnd, VB3)
    circuit.I("IB1", circuit.gnd, "IB1", IB1)
    circuit.V("VCLK1", "VCLK1", circuit.gnd, VCLK1)
    circuit.V("VCLK2", "VCLK2", circuit.gnd, VCLK2)

    # Model names, W/L dimensions, and passive values are variables.
    circuit.MOSFET("11", "VIN1", "VCLK1", "net29", circuit.gnd, model=NMOS_MODEL, width=W_M11, length=L_M11)
    circuit.MOSFET("10", "net29", "VCLK2", "net41", circuit.gnd, model=NMOS_MODEL, width=W_M10, length=L_M10)
    circuit.MOSFET("9", "net35", "net41", "IB1", circuit.gnd, model=NMOS_MODEL, width=W_M9, length=L_M9)
    circuit.MOSFET("8", "net36", circuit.gnd, "IB1", circuit.gnd, model=NMOS_MODEL, width=W_M8, length=L_M8)
    circuit.MOSFET("5", "VOUT1", "VB2", "net39", circuit.gnd, model=NMOS_MODEL, width=W_M5, length=L_M5)
    circuit.MOSFET("4", "net31", "VB2", "net40", circuit.gnd, model=NMOS_MODEL, width=W_M4, length=L_M4)
    circuit.MOSFET("7", "net39", "net31", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M7, length=L_M7)
    circuit.MOSFET("6", "net40", "net31", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M6, length=L_M6)
    circuit.C("0", "net41", "net29", C0)
    circuit.C("4", "net41", "VOUT1", C4)
    circuit.MOSFET("1", "net35", "VB3", "VDD", "VDD", model=PMOS_MODEL, width=W_M1, length=L_M1)
    circuit.MOSFET("0", "net36", "VB3", "VDD", "VDD", model=PMOS_MODEL, width=W_M0, length=L_M0)
    circuit.MOSFET("3", "VOUT1", "VB1", "net35", "VDD", model=PMOS_MODEL, width=W_M3, length=L_M3)
    circuit.MOSFET("2", "net31", "VB1", "net36", "VDD", model=PMOS_MODEL, width=W_M2, length=L_M2)
    return circuit
