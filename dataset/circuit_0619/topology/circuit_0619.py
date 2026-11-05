"""Generated from circuit_0619.cir by cadence_to_pyspice.py.

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
    VHOLD1: Any,
    VTRACK1: Any,
    NPN_MODEL: Any,
    R6: Any,
    R5: Any,
    R4: Any,
    R3: Any,
    R0: Any,
    R1: Any,
    R2: Any,
    C3: Any,
    C2: Any,
    C1: Any,
    C0: Any,
) -> Circuit:
    circuit = Circuit("AnalogGenie 765 / circuit_0619")

    # VSS is treated as the PySpice ground node. Other source values
    # are caller-supplied variables because the dataset has no values.
    circuit.V("VDD", "VDD", circuit.gnd, VDD)
    circuit.V("VIN1", "VIN1", circuit.gnd, VIN1)
    circuit.V("VIN2", "VIN2", circuit.gnd, VIN2)
    circuit.V("VHOLD1", "VHOLD1", circuit.gnd, VHOLD1)
    circuit.V("VTRACK1", "VTRACK1", circuit.gnd, VTRACK1)

    # Model names, W/L dimensions, and passive values are variables.
    circuit.raw_spice += "Q11 net16 VTRACK1 net14 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q10 VOUT2 VTRACK1 net14 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q9 net13 VHOLD1 net14 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q8 VDD net16 VOUT2 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q7 VDD net13 net16 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q1 net4 VTRACK1 net7 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q0 VOUT1 VTRACK1 net7 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q2 net9 VHOLD1 net7 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q3 VDD net4 VOUT1 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q4 VDD net9 net4 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q5 net13 VIN2 net25 0 " + str(NPN_MODEL) + "\n"
    circuit.raw_spice += "Q6 net9 VIN1 net26 0 " + str(NPN_MODEL) + "\n"
    circuit.R("6", "net24", circuit.gnd, R6)
    circuit.R("5", "net14", circuit.gnd, R5)
    circuit.R("4", "net7", circuit.gnd, R4)
    circuit.R("3", "VDD", "net9", R3)
    circuit.R("0", "net26", "net24", R0)
    circuit.R("1", "net25", "net24", R1)
    circuit.R("2", "VDD", "net13", R2)
    circuit.C("3", "net9", "net16", C3)
    circuit.C("2", "VDD", "net16", C2)
    circuit.C("1", "net13", "net4", C1)
    circuit.C("0", "VDD", "net4", C0)
    return circuit
