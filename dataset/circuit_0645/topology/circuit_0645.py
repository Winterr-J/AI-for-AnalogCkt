"""Generated from circuit_0645.cir by cadence_to_pyspice.py.

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
    VIN3: Any,
    VB1: Any,
    VB2: Any,
    VB3: Any,
    IB1: Any,
    VCLK1: Any,
    VCLK2: Any,
    NMOS_MODEL: Any,
    PMOS_MODEL: Any,
    W_M21: Any,
    L_M21: Any,
    W_M20: Any,
    L_M20: Any,
    W_M19: Any,
    L_M19: Any,
    W_M18: Any,
    L_M18: Any,
    W_M17: Any,
    L_M17: Any,
    W_M16: Any,
    L_M16: Any,
    W_M13: Any,
    L_M13: Any,
    W_M12: Any,
    L_M12: Any,
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
    W_M1: Any,
    L_M1: Any,
    W_M0: Any,
    L_M0: Any,
    W_M3: Any,
    L_M3: Any,
    W_M2: Any,
    L_M2: Any,
    C4: Any,
    C2: Any,
    C3: Any,
    C0: Any,
) -> Circuit:
    circuit = Circuit("AnalogGenie 791 / circuit_0645")

    # VSS is treated as the PySpice ground node. Other source values
    # are caller-supplied variables because the dataset has no values.
    circuit.V("VDD", "VDD", circuit.gnd, VDD)
    circuit.V("VIN1", "VIN1", circuit.gnd, VIN1)
    circuit.V("VIN2", "VIN2", circuit.gnd, VIN2)
    circuit.V("VIN3", "VIN3", circuit.gnd, VIN3)
    circuit.V("VB1", "VB1", circuit.gnd, VB1)
    circuit.V("VB2", "VB2", circuit.gnd, VB2)
    circuit.V("VB3", "VB3", circuit.gnd, VB3)
    circuit.I("IB1", circuit.gnd, "IB1", IB1)
    circuit.V("VCLK1", "VCLK1", circuit.gnd, VCLK1)
    circuit.V("VCLK2", "VCLK2", circuit.gnd, VCLK2)

    # Model names, W/L dimensions, and passive values are variables.
    circuit.MOSFET("21", "net73", "VCLK2", "net75", circuit.gnd, model=NMOS_MODEL, width=W_M21, length=L_M21)
    circuit.MOSFET("20", "VIN3", "VCLK1", "net66", circuit.gnd, model=NMOS_MODEL, width=W_M20, length=L_M20)
    circuit.MOSFET("19", "net74", "VCLK2", "net75", circuit.gnd, model=NMOS_MODEL, width=W_M19, length=L_M19)
    circuit.MOSFET("18", "VIN2", "VCLK1", "net68", circuit.gnd, model=NMOS_MODEL, width=W_M18, length=L_M18)
    circuit.MOSFET("17", "net74", "VCLK1", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M17, length=L_M17)
    circuit.MOSFET("16", "net68", "VCLK2", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M16, length=L_M16)
    circuit.MOSFET("13", "net73", "VCLK1", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M13, length=L_M13)
    circuit.MOSFET("12", "net66", "VCLK2", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M12, length=L_M12)
    circuit.MOSFET("9", "net81", "net75", "IB1", circuit.gnd, model=NMOS_MODEL, width=W_M9, length=L_M9)
    circuit.MOSFET("8", "net86", circuit.gnd, "IB1", circuit.gnd, model=NMOS_MODEL, width=W_M8, length=L_M8)
    circuit.MOSFET("5", "VOUT1", "VB2", "net88", circuit.gnd, model=NMOS_MODEL, width=W_M5, length=L_M5)
    circuit.MOSFET("4", "net77", "VB2", "net89", circuit.gnd, model=NMOS_MODEL, width=W_M4, length=L_M4)
    circuit.MOSFET("7", "net88", "net77", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M7, length=L_M7)
    circuit.MOSFET("6", "net89", "net77", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M6, length=L_M6)
    circuit.MOSFET("1", "net81", "VB3", "VDD", "VDD", model=PMOS_MODEL, width=W_M1, length=L_M1)
    circuit.MOSFET("0", "net86", "VB3", "VDD", "VDD", model=PMOS_MODEL, width=W_M0, length=L_M0)
    circuit.MOSFET("3", "VOUT1", "VB1", "net81", "VDD", model=PMOS_MODEL, width=W_M3, length=L_M3)
    circuit.MOSFET("2", "net77", "VB1", "net86", "VDD", model=PMOS_MODEL, width=W_M2, length=L_M2)
    circuit.C("4", "net75", "VOUT1", C4)
    circuit.C("2", "net73", "net66", C2)
    circuit.C("3", "net74", "net68", C3)
    circuit.C("0", "net75", "VIN1", C0)
    return circuit
