"""Generated from circuit_0616.cir by cadence_to_pyspice.py.

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
    C1: Any,
    C0: Any,
    W_M3: Any,
    L_M3: Any,
    W_M2: Any,
    L_M2: Any,
    W_M1: Any,
    L_M1: Any,
    W_M0: Any,
    L_M0: Any,
    W_M14: Any,
    L_M14: Any,
    W_M15: Any,
    L_M15: Any,
    W_M20: Any,
    L_M20: Any,
    W_M16: Any,
    L_M16: Any,
    W_M18: Any,
    L_M18: Any,
    W_M17: Any,
    L_M17: Any,
    W_M23: Any,
    L_M23: Any,
    W_M21: Any,
    L_M21: Any,
    W_M22: Any,
    L_M22: Any,
    W_M19: Any,
    L_M19: Any,
) -> Circuit:
    circuit = Circuit("AnalogGenie 762 / circuit_0616")

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
    circuit.C("1", "net07", "net06", C1)
    circuit.C("0", "VOUT1", "net045", C0)
    circuit.MOSFET("3", "net06", "VCLK1", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M3, length=L_M3)
    circuit.MOSFET("2", "net045", "VCLK2", "net06", circuit.gnd, model=NMOS_MODEL, width=W_M2, length=L_M2)
    circuit.MOSFET("1", "net07", "VCLK1", "VIN1", circuit.gnd, model=NMOS_MODEL, width=W_M1, length=L_M1)
    circuit.MOSFET("0", "VOUT1", "VCLK2", "net07", circuit.gnd, model=NMOS_MODEL, width=W_M0, length=L_M0)
    circuit.MOSFET("14", "net041", "net045", "IB1", "net046", model=NMOS_MODEL, width=W_M14, length=L_M14)
    circuit.MOSFET("15", "net042", circuit.gnd, "IB1", "net046", model=NMOS_MODEL, width=W_M15, length=L_M15)
    circuit.MOSFET("20", "VOUT1", "VB1", "net043", circuit.gnd, model=NMOS_MODEL, width=W_M20, length=L_M20)
    circuit.MOSFET("16", "net038", "VB1", "net047", circuit.gnd, model=NMOS_MODEL, width=W_M16, length=L_M16)
    circuit.MOSFET("18", "net043", "net038", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M18, length=L_M18)
    circuit.MOSFET("17", "net047", "net038", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M17, length=L_M17)
    circuit.MOSFET("23", "net042", "VB3", "VDD", "VDD", model=PMOS_MODEL, width=W_M23, length=L_M23)
    circuit.MOSFET("21", "net041", "VB3", "VDD", "VDD", model=PMOS_MODEL, width=W_M21, length=L_M21)
    circuit.MOSFET("22", "net038", "VB2", "net042", "VDD", model=PMOS_MODEL, width=W_M22, length=L_M22)
    circuit.MOSFET("19", "VOUT1", "VB2", "net041", "VDD", model=PMOS_MODEL, width=W_M19, length=L_M19)
    return circuit
