"""Generated from circuit_0476.cir by cadence_to_pyspice.py.

All values absent from the source netlist remain required variables.
"""

from __future__ import annotations

from typing import Any

from PySpice.Spice.Netlist import Circuit


def build_circuit(
    *,
    IOUT1: Any,
    IB1: Any,
    NMOS_MODEL: Any,
    W_M1: Any,
    L_M1: Any,
    W_M0: Any,
    L_M0: Any,
    W_M4: Any,
    L_M4: Any,
    W_M3: Any,
    L_M3: Any,
) -> Circuit:
    circuit = Circuit("AnalogGenie current mirrors 612 (renumbered circuit_0476)")

    # VSS is treated as the PySpice ground node. Other source values
    # are caller-supplied variables because the dataset has no values.
    circuit.V("IOUT1", "IOUT1", circuit.gnd, IOUT1)
    circuit.I("IB1", circuit.gnd, "IB1", IB1)

    # Model names, W/L dimensions, and passive values are variables.
    circuit.MOSFET("1", "net011", "IB1", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M1, length=L_M1)
    circuit.MOSFET("0", "net012", "IB1", circuit.gnd, circuit.gnd, model=NMOS_MODEL, width=W_M0, length=L_M0)
    circuit.MOSFET("4", "IB1", "IB1", "net012", circuit.gnd, model=NMOS_MODEL, width=W_M4, length=L_M4)
    circuit.MOSFET("3", "IOUT1", "IB1", "net011", circuit.gnd, model=NMOS_MODEL, width=W_M3, length=L_M3)
    return circuit
