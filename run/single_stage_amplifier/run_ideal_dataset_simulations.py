"""Bias and validate all AnalogGenie single-stage circuits with ideal models."""

from __future__ import annotations

import argparse
import copy
import ctypes
import ctypes.util
import itertools
import math
import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import yaml
from PySpice.Spice.Netlist import Circuit


AG_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = AG_ROOT.parent
SOURCE_ROOT = PROJECT_ROOT / "AnalogGenie" / "Dataset"
if str(AG_ROOT) not in sys.path:
    sys.path.insert(0, str(AG_ROOT))

from conversion.cadence_to_pyspice import (  # noqa: E402
    CadenceDevice,
    parse_cadence_netlist,
    parse_ports,
)
from simulation_metrics import (  # noqa: E402
    run_ac_analysis,
    run_dc_sweep,
    run_operating_point,
    run_transient_analysis,
)


ORIGINAL_IDS = [
    4, 6, 9, 14, 17, 20, 22, 24, 27, 29, 31, 35, 37, 38,
    41, 43, 46, 48, 50, 52, 54, 57, 59, 61, 63, 65, 67,
]

POLARITY = {
    9: "inverting", 14: "inverting", 17: "inverting", 20: "noninverting",
    22: "noninverting", 24: "noninverting", 27: "inverting",
    29: "noninverting", 31: "noninverting", 35: "inverting",
    37: "inverting", 38: "inverting", 41: "inverting", 43: "inverting",
    46: "noninverting", 48: "inverting", 50: "noninverting",
    52: "inverting", 54: "inverting", 57: "noninverting", 59: "inverting",
    61: "inverting", 63: "inverting", 65: "inverting", 67: "inverting",
}

_DLL_HANDLE = None
_PRELOADED_DLLS: list[Any] = []


@dataclass
class Candidate:
    vin: float
    biases: dict[str, float]
    currents: dict[str, float]
    node_voltages: dict[str, float]
    supply_current: float | None
    conducting_fraction: float
    output_margin: float | None
    score: float


def _resolve_ngspice_library(system: str) -> tuple[str, bool]:
    """Return a shared-library name/path and whether it is the bundled Windows DLL."""
    override = os.getenv("NGSPICE_LIBRARY_PATH")
    if override is not None:
        if not override.strip():
            raise ValueError("NGSPICE_LIBRARY_PATH must not be empty")
        library = Path(override).expanduser()
        if library.is_absolute():
            if not library.is_file():
                raise FileNotFoundError(f"ngspice shared library not found: {library}")
            return str(library.resolve()), False
        if "/" in override or "\\" in override:
            raise ValueError("NGSPICE_LIBRARY_PATH must be an absolute path or a library name")
        return override, False

    if system == "Windows":
        library = (
            PROJECT_ROOT / "Batch_simulation" / "vendor" / "Spice64_dll"
            / "dll-vs" / "ngspice.dll"
        )
        if not library.is_file():
            raise FileNotFoundError(
                f"ngspice shared library not found: {library}; "
                "set NGSPICE_LIBRARY_PATH to an installed ngspice DLL"
            )
        return str(library), True

    if system == "Linux":
        library = ctypes.util.find_library("ngspice")
        if not library:
            raise FileNotFoundError(
                "ngspice shared library not found; install libngspice0 or set "
                "NGSPICE_LIBRARY_PATH (the ngspice executable is not sufficient)"
            )
        return library, False

    raise RuntimeError(f"Unsupported platform for ngspice shared library: {system}")


def configure_ngspice() -> str:
    global _DLL_HANDLE
    from PySpice.Spice.NgSpice.Shared import NgSpiceShared

    system = platform.system()
    library, bundled_windows = _resolve_ngspice_library(system)
    if system == "Linux":
        try:
            ctypes.CDLL(library)
        except OSError as error:
            raise FileNotFoundError(
                f"ngspice shared library could not be loaded: {library}; "
                "check libngspice0 and its dependencies"
            ) from error
    if system == "Windows" and hasattr(os, "add_dll_directory"):
        library_path = Path(library)
        if not library_path.is_absolute():
            # A bare DLL name is resolved by Windows and needs no directory hint.
            NgSpiceShared.LIBRARY_PATH = library
            return library
        if _DLL_HANDLE is None:
            _DLL_HANDLE = os.add_dll_directory(str(library_path.parent))
        dependency = library_path.parent / "libomp140.x86_64.dll"
        if bundled_windows and not dependency.is_file():
            raise FileNotFoundError(dependency)
        if dependency.is_file() and not _PRELOADED_DLLS:
            _PRELOADED_DLLS.append(ctypes.WinDLL(str(dependency)))
        if bundled_windows:
            os.environ.setdefault(
                "SPICE_LIB_DIR",
                str(PROJECT_ROOT / "Batch_simulation" / "vendor" / "ngspice_runtime"),
            )
    NgSpiceShared.LIBRARY_PATH = library
    return library


def clean(value: Any) -> Any:
    if isinstance(value, (np.floating, np.integer)):
        value = value.item()
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, np.ndarray):
        return [clean(item) for item in value.tolist()]
    if isinstance(value, dict):
        return {str(key): clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(item) for item in value]
    return value


def dump_yaml(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(clean(payload), sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8",
    )


def scalar(vector: Any) -> float:
    values = np.asarray(vector, dtype=float).reshape(-1)
    if not len(values):
        raise ValueError("empty simulator vector")
    return float(values[0])


def optional_scalar(vector: Any | None) -> float | None:
    if vector is None:
        return None
    values = np.asarray(vector, dtype=float).reshape(-1)
    return float(values[0]) if len(values) else None


def nodes(analysis: Any) -> dict[str, float]:
    return {str(name): scalar(vector) for name, vector in analysis.nodes.items()}


def lookup(mapping: Any, name: str) -> Any | None:
    target = name.casefold()
    for key, value in mapping.items():
        if str(key).casefold() == target:
            return value
    return None


def source_data(source_id: int) -> tuple[list[CadenceDevice], list[str]]:
    source_dir = SOURCE_ROOT / str(source_id)
    devices = parse_cadence_netlist(
        (source_dir / f"{source_id}.cir").read_text(encoding="utf-8")
    )
    ports = parse_ports(
        (source_dir / f"Port{source_id}.txt").read_text(encoding="utf-8")
    )
    return devices, ports


def build_circuit(
    circuit_id: str,
    devices: list[CadenceDevice],
    ports: list[str],
    global_config: dict[str, Any],
    *,
    vin: float,
    biases: dict[str, float],
    currents: dict[str, float],
    transient: bool = False,
) -> Circuit:
    model_defaults = global_config["level1_model_defaults"]
    device_defaults = global_config["device_defaults"]
    circuit = Circuit(f"{circuit_id} ideal validation")
    for dtype, model_name in (("nmos", "NMOS_IDEAL"), ("pmos", "PMOS_IDEAL")):
        model = model_defaults[dtype]
        circuit.model(
            model_name,
            dtype.upper(),
            LEVEL=int(model["level"]),
            VTO=float(model["VTO"]),
            KP=float(model["KP"]) * 1e-6,
            LAMBDA=float(model["LAMBDA"]),
        )

    vdd = 3.3
    if "VDD" in ports:
        circuit.V("VDD", "VDD", circuit.gnd, vdd)
    if any(port.upper().startswith("VIN") for port in ports):
        frequency_selective = any(device.kind == "capacitor" for device in devices)
        transient_frequency_hz = 100e6 if frequency_selective else 10e3
        source_value = (
            f"DC {vin:.12g} AC 1 SIN({vin:.12g} 0.001 {transient_frequency_hz:.12g})"
            if transient
            else f"DC {vin:.12g} AC 1"
        )
        for port in ports:
            if port.upper().startswith("VIN"):
                circuit.V(port, port, circuit.gnd, source_value)
    for name, value in biases.items():
        circuit.V(name, name, circuit.gnd, float(value))
    for name, value in currents.items():
        # IB ports in these schematics are bias-current sinks.  The generated
        # topology modules need this correction to obtain physical DC nodes.
        circuit.I(name, name, circuit.gnd, float(value))

    for device in devices:
        device_nodes = [
            circuit.gnd if node.upper() in {"0", "GND", "VSS"} else node
            for node in device.nodes
        ]
        suffix = device.name[1:] if len(device.name) > 1 else device.name
        if device.kind in {"nmos4", "pmos4"}:
            dtype = "nmos" if device.kind == "nmos4" else "pmos"
            defaults = device_defaults[dtype]
            model_name = "NMOS_IDEAL" if dtype == "nmos" else "PMOS_IDEAL"
            circuit.MOSFET(
                suffix,
                *device_nodes,
                model=model_name,
                width=float(defaults["W"]) * 1e-6,
                length=float(defaults["L"]) * 1e-6,
                multiplier=int(defaults["m"]),
            )
        elif device.kind == "resistor":
            circuit.R(suffix, *device_nodes, float(device_defaults["resistor"]["R"]))
        elif device.kind == "capacitor":
            circuit.C(suffix, *device_nodes, float(device_defaults["capacitor"]["C"]))
        else:
            raise ValueError(device.kind)
    circuit.raw_spice += ".options noacct\n"
    return circuit


def candidate_values(ports: list[str]) -> Iterable[tuple[dict[str, float], dict[str, float]]]:
    voltage_names = [port for port in ports if port.upper().startswith("VB")]
    current_names = [port for port in ports if port.upper().startswith("IB")]
    voltage_grid = [0.5, 1.0, 1.65, 2.3, 2.8]
    current_grid = [1e-6, 10e-6, 100e-6, 1e-3]

    # A common-value pass finds useful points cheaply; Cartesian combinations
    # are only needed for multi-bias cascodes/folded structures.
    common: list[tuple[dict[str, float], dict[str, float]]] = []
    for value in voltage_grid:
        common.append(({name: value for name in voltage_names}, {name: 10e-6 for name in current_names}))
    for current in current_grid:
        common.append(({name: 1.65 for name in voltage_names}, {name: current for name in current_names}))

    detailed = (
        (
            dict(zip(voltage_names, voltage_values)),
            dict(zip(current_names, current_values)),
        )
        for voltage_values in itertools.product(voltage_grid, repeat=len(voltage_names))
        for current_values in itertools.product(current_grid, repeat=len(current_names))
    )
    seen: set[tuple[Any, ...]] = set()
    for biases, currents in itertools.chain(common, detailed):
        key = tuple(sorted(biases.items())) + tuple(sorted(currents.items()))
        if key not in seen:
            seen.add(key)
            yield biases, currents


def evaluate_candidate(
    circuit_id: str,
    devices: list[CadenceDevice],
    ports: list[str],
    config: dict[str, Any],
    *,
    vin: float,
    biases: dict[str, float],
    currents: dict[str, float],
) -> Candidate | None:
    try:
        circuit = build_circuit(
            circuit_id, devices, ports, config,
            vin=vin, biases=biases, currents=currents,
        )
        analysis = circuit.simulator(temperature=25, nominal_temperature=25).operating_point()
        node_values = nodes(analysis)
        supply_vector = lookup(analysis.branches, "vvdd")
        supply_current = scalar(supply_vector) if supply_vector is not None else None
        mos_names = [device.name for device in devices if device.kind in {"nmos4", "pmos4"}]
        conducting = 0
        internal = None
        if mos_names:
            internal_simulator = circuit.simulator(temperature=25, nominal_temperature=25)
            internal_simulator.save_internal_parameters(
                *[f"@{name}[id]" for name in mos_names]
            )
            internal = internal_simulator.operating_point()
            for name in mos_names:
                vector = lookup(internal.internal_parameters, f"@{name}[id]")
                if vector is not None and abs(scalar(vector)) >= 1e-9:
                    conducting += 1
        conducting_fraction = conducting / len(mos_names) if mos_names else 1.0
        output = next(
            (value for name, value in node_values.items() if name.casefold() == "vout1"),
            None,
        )
        output_margin = min(output, 3.3 - output) if output is not None else None
        output_score = output_margin / 1.65 if output_margin is not None else 0.5
        physical_nodes = [
            value for name, value in node_values.items()
            if not name.casefold().startswith("vdd")
        ]
        nodes_within_rails = all(-1e-3 <= value <= 3.301 for value in physical_nodes)
        current_penalty = (
            0.0 if supply_current is None else max(0.0, abs(supply_current) - 0.01) * 100.0
        )
        score = (
            4.0 * conducting_fraction
            + 2.0 * output_score
            + (3.0 if nodes_within_rails else -6.0)
            - current_penalty
        )
        return Candidate(
            vin=vin,
            biases=biases,
            currents=currents,
            node_voltages=node_values,
            supply_current=supply_current,
            conducting_fraction=conducting_fraction,
            output_margin=output_margin,
            score=score,
        )
    except Exception:
        return None


def dc_sweep(
    circuit_id: str,
    source_id: int,
    devices: list[CadenceDevice],
    ports: list[str],
    config: dict[str, Any],
    candidate: Candidate,
) -> tuple[dict[str, Any], float]:
    if not any(port.upper().startswith("VIN") for port in ports):
        return ({"status": "NOT_APPLICABLE", "reason": "no input voltage port"}, candidate.vin)
    if not any(port.upper().startswith("VOUT") for port in ports):
        return ({"status": "NOT_APPLICABLE", "reason": "no output voltage port"}, candidate.vin)
    if source_id == 29:
        return ({
            "status": "NOT_APPLICABLE",
            "reason": "input is AC-coupled; a DC VIN sweep is blocked by the coupling capacitor",
        }, candidate.vin)
    circuit = build_circuit(
        circuit_id, devices, ports, config,
        vin=candidate.vin, biases=candidate.biases, currents=candidate.currents,
    )
    expected = POLARITY[source_id]
    result = run_dc_sweep(
        circuit,
        start_v=0.0,
        stop_v=3.3,
        step_v=0.01,
        vdd_v=3.3,
        expected_polarity=expected,
    )
    vin_values = result.input_voltage_v
    vout_values = result.output_voltage_v
    gain = result.local_gain_v_per_v
    direction = gain < -0.1 if expected == "inverting" else gain > 0.1
    away = (vout_values > 0.165) & (vout_values < 3.135)
    normal = direction & away
    indices = np.flatnonzero(normal)
    if len(indices):
        chosen = indices[int(np.argmin(np.abs(vout_values[indices] - 1.65)))]
        recommended = float(result.recommended_bias_v or vin_values[chosen])
    else:
        recommended = candidate.vin
    observed = None
    if len(indices):
        observed = "inverting" if float(np.median(gain[indices])) < 0 else "noninverting"
    payload = {
        "status": "PASS" if len(indices) else "WARN",
        "analysis": "DC input transfer sweep",
        "sweep": {"source": "VIN1", "start_v": 0.0, "stop_v": 3.3, "step_v": 0.01},
        "expected_polarity": expected,
        "observed_polarity": observed,
        "normal_amplification_found": bool(len(indices)),
        "recommended_input_bias_v": recommended,
        "output_min_v": float(np.min(vout_values)),
        "output_max_v": float(np.max(vout_values)),
        "maximum_abs_local_gain_v_per_v": float(np.max(np.abs(gain))),
        "normal_input_range_v": (
            {"minimum": float(vin_values[indices[0]]), "maximum": float(vin_values[indices[-1]])}
            if len(indices) else None
        ),
        "point_count": int(len(vin_values)),
    }
    return payload, recommended


def detailed_op(
    circuit_id: str,
    devices: list[CadenceDevice],
    ports: list[str],
    config: dict[str, Any],
    candidate: Candidate,
) -> dict[str, Any]:
    circuit = build_circuit(
        circuit_id, devices, ports, config,
        vin=candidate.vin, biases=candidate.biases, currents=candidate.currents,
    )
    op_result = run_operating_point(
        circuit,
        vdd_v=3.3,
        supply_source="VVDD",
        bias_nodes=[],
        intermediate_nodes=[],
    )
    node_values = op_result.node_voltages_v
    supply_current = op_result.supply_current_a
    output = next((v for k, v in node_values.items() if k.casefold() == "vout1"), None)
    mos_names = [device.name for device in devices if device.kind in {"nmos4", "pmos4"}]
    parameters = ("id", "gm", "vgs", "vds", "vth", "vdsat")
    mos_points = op_result.mos_operating_points
    all_conducting = all(point["conducting"] for point in mos_points.values())
    output_ok = output is None or 0.165 < output < 3.135
    current_ok = supply_current is None or abs(supply_current) <= 0.1
    return {
        "status": "PASS" if all_conducting and output_ok and current_ok else "WARN",
        "analysis": "operating_point",
        "node_voltages_v": node_values,
        "supply_current_a": supply_current,
        "mos_operating_points": mos_points,
        "checks": {
            "all_mos_conducting": all_conducting,
            "output_not_at_rail": output_ok if output is not None else None,
            "supply_current_not_abnormal": current_ok,
        },
        "warnings": list(op_result.warnings),
    }


def ac_analysis(
    circuit_id: str,
    devices: list[CadenceDevice],
    ports: list[str],
    config: dict[str, Any],
    candidate: Candidate,
) -> dict[str, Any]:
    if not any(port.upper().startswith("VIN") for port in ports):
        return {"status": "NOT_APPLICABLE", "reason": "no input voltage port"}
    if not any(port.upper().startswith("VOUT") for port in ports):
        return {"status": "NOT_APPLICABLE", "reason": "no output voltage port"}
    circuit = build_circuit(
        circuit_id, devices, ports, config,
        vin=candidate.vin, biases=candidate.biases, currents=candidate.currents,
    )
    result = run_ac_analysis(
        circuit,
        start_frequency_hz=1.0,
        stop_frequency_hz=1e9,
        points_per_decade=40,
    )
    frequency = result.frequency_hz
    gain = result.gain_v_per_v
    gain_db = result.gain_db
    phase = result.phase_deg
    low_db = result.low_frequency_gain_db
    low_gain = result.low_frequency_gain_v_per_v
    bandwidth = result.bandwidth_3db_hz
    peak_index = int(np.argmax(gain))
    peak_gain = float(gain[peak_index])
    peak_frequency = float(frequency[peak_index])
    ac_coupled = any(device.kind == "capacitor" and any(
        node.upper().startswith("VIN") for node in device.nodes
    ) for device in devices)
    frequency_selective = any(device.kind == "capacitor" for device in devices)
    usable_gain = peak_gain if frequency_selective else low_gain
    return {
        "status": "PASS" if usable_gain >= 0.1 else "WARN",
        "analysis": "AC small-signal frequency response",
        "frequency_sweep": {"start_hz": 1.0, "stop_hz": 1e9, "points_per_decade": 40},
        "low_frequency_gain_v_per_v": low_gain,
        "low_frequency_gain_db": low_db,
        "low_frequency_phase_deg": float(phase[0]),
        "peak_gain_v_per_v": peak_gain,
        "peak_gain_frequency_hz": peak_frequency,
        "ac_coupled_input": ac_coupled,
        "frequency_selective_topology": frequency_selective,
        "bandwidth_3db_hz": bandwidth,
        "gain_db_at_stop_frequency": float(gain_db[-1]),
        "point_count": int(len(frequency)),
    }


def transient_check(
    circuit_id: str,
    source_id: int,
    devices: list[CadenceDevice],
    ports: list[str],
    config: dict[str, Any],
    candidate: Candidate,
) -> dict[str, Any]:
    if not any(port.upper().startswith("VIN") for port in ports):
        return {"status": "NOT_APPLICABLE", "reason": "no input voltage port"}
    if not any(port.upper().startswith("VOUT") for port in ports):
        return {"status": "NOT_APPLICABLE", "reason": "no output voltage port"}
    frequency_hz = 100e6 if source_id in {27, 29} else 10e3
    circuit = build_circuit(
        circuit_id, devices, ports, config,
        vin=candidate.vin, biases=candidate.biases, currents=candidate.currents,
        transient=True,
    )
    period = 1.0 / frequency_hz
    result = run_transient_analysis(
        circuit,
        step_time_s=period / 50.0,
        end_time_s=period * 20.0,
        expected_polarity=POLARITY[source_id],
    )
    vin_values = result.input_voltage_v
    vout_values = result.output_voltage_v
    signed_gain = result.signed_gain_v_per_v
    expected = POLARITY[source_id]
    polarity_ok = bool(result.polarity_matches_expected)
    return {
        "status": "PASS" if polarity_ok and abs(signed_gain) >= 0.05 else "WARN",
        "analysis": "transient functional check",
        "input": {"bias_v": candidate.vin, "amplitude_v": 0.001, "frequency_hz": frequency_hz},
        "signed_gain_v_per_v": signed_gain,
        "expected_polarity": expected,
        "polarity_matches": polarity_ok,
        "output_min_v": float(np.min(vout_values)),
        "output_max_v": float(np.max(vout_values)),
        "point_count": int(len(vout_values)),
    }


def parameter_payload(
    circuit_id: str,
    source_id: int,
    devices: list[CadenceDevice],
    ports: list[str],
    config: dict[str, Any],
    candidate: Candidate,
    checks: dict[str, Any],
) -> dict[str, Any]:
    models = config["level1_model_defaults"]
    defaults = config["device_defaults"]
    instance_parameters: dict[str, Any] = {}
    for device in devices:
        if device.kind == "nmos4":
            instance_parameters[device.name] = {
                "model": "NMOS_IDEAL", **copy.deepcopy(defaults["nmos"]),
                "W_unit": "um", "L_unit": "um",
            }
        elif device.kind == "pmos4":
            instance_parameters[device.name] = {
                "model": "PMOS_IDEAL", **copy.deepcopy(defaults["pmos"]),
                "W_unit": "um", "L_unit": "um",
            }
        elif device.kind == "resistor":
            instance_parameters[device.name] = {
                "model": "ideal_resistor", "R": defaults["resistor"]["R"], "R_unit": "Ohm",
            }
        elif device.kind == "capacitor":
            instance_parameters[device.name] = {
                "model": "ideal_capacitor", "C": defaults["capacitor"]["C"], "C_unit": "F",
            }
    operating: dict[str, Any] = {}
    if "VDD" in ports:
        operating["VDD"] = {"value": 3.3, "unit": "V"}
    if "VSS" in ports:
        operating["VSS"] = {"value": 0.0, "unit": "V", "fixed": True}
    for port in ports:
        if port.upper().startswith("VIN"):
            operating[f"{port}_DC"] = {"value": candidate.vin, "unit": "V"}
    for name, value in candidate.biases.items():
        operating[name] = {"value": value, "unit": "V"}
    for name, value in candidate.currents.items():
        operating[name] = {"value": value, "unit": "A", "direction": "node_to_VSS"}
    return {
        "schema_version": 1,
        "circuit_id": circuit_id,
        "analoggenie_folder_id": source_id,
        "simulation_model": "ideal_level1",
        "source_of_initial_values": "dataset/global/ideal_global.yaml and operating_global.yaml",
        "model_parameters": {
            "NMOS_IDEAL": {**copy.deepcopy(models["nmos"]), "KP_unit": "uA/V^2", "VTO_unit": "V", "LAMBDA_unit": "1/V"},
            "PMOS_IDEAL": {**copy.deepcopy(models["pmos"]), "KP_unit": "uA/V^2", "VTO_unit": "V", "LAMBDA_unit": "1/V"},
        },
        "device_parameters": instance_parameters,
        "operating_parameters": operating,
        "functional_validation": checks,
    }


def search_candidate(
    circuit_id: str,
    devices: list[CadenceDevice],
    ports: list[str],
    config: dict[str, Any],
) -> Candidate:
    has_input = any(port.upper().startswith("VIN") for port in ports)
    vin_grid = [1.2] if not has_input else [0.6, 1.0, 1.2, 1.65, 2.1, 2.7]
    best: Candidate | None = None
    for biases, currents in candidate_values(ports):
        for vin in vin_grid:
            candidate = evaluate_candidate(
                circuit_id, devices, ports, config,
                vin=vin, biases=biases, currents=currents,
            )
            if candidate is not None and (best is None or candidate.score > best.score):
                best = candidate
            if (
                candidate is not None
                and candidate.conducting_fraction == 1.0
                and (candidate.output_margin is None or candidate.output_margin > 0.35)
                and all(
                    -1e-3 <= value <= 3.301
                    for name, value in candidate.node_voltages.items()
                    if not name.casefold().startswith("vdd")
                )
            ):
                return candidate
    if best is None:
        raise RuntimeError(f"no converged operating point for {circuit_id}")
    return best


def run_one(
    output_root: Path,
    index: int,
    source_id: int,
    config: dict[str, Any],
) -> dict[str, Any]:
    circuit_id = f"circuit_{index:04d}"
    devices, ports = source_data(source_id)
    candidate = search_candidate(circuit_id, devices, ports, config)
    dc_payload, recommended_vin = dc_sweep(
        circuit_id, source_id, devices, ports, config, candidate
    )
    if dc_payload["status"] == "PASS" and abs(recommended_vin - candidate.vin) > 1e-12:
        refined = evaluate_candidate(
            circuit_id, devices, ports, config,
            vin=recommended_vin, biases=candidate.biases, currents=candidate.currents,
        )
        if refined is not None and all(
            -1e-3 <= value <= 3.301
            for name, value in refined.node_voltages.items()
            if not name.casefold().startswith("vdd")
        ):
            candidate = refined
    op_payload = detailed_op(circuit_id, devices, ports, config, candidate)
    ac_payload = ac_analysis(circuit_id, devices, ports, config, candidate)
    tran_payload = transient_check(circuit_id, source_id, devices, ports, config, candidate)
    checks = {
        "op_status": op_payload["status"],
        "dc_status": dc_payload["status"],
        "ac_status": ac_payload["status"],
        "transient_status": tran_payload["status"],
    }
    parameter_dir = output_root / circuit_id / "simulation" / "parameter"
    ideal_result_dir = output_root / circuit_id / "simulation" / "results" / "ideal"
    dump_yaml(
        parameter_dir / "ideal_param.yaml",
        parameter_payload(circuit_id, source_id, devices, ports, config, candidate, checks),
    )
    dump_yaml(ideal_result_dir / "op_results.yaml", op_payload)
    dump_yaml(ideal_result_dir / "dc_results.yaml", dc_payload)
    dump_yaml(ideal_result_dir / "ac_results.yaml", ac_payload)
    return {"circuit_id": circuit_id, "source_id": source_id, **checks}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_root", type=Path)
    parser.add_argument(
        "--global-root",
        type=Path,
        default=AG_ROOT / "dataset" / "global",
    )
    args = parser.parse_args()
    output_root = args.output_root.resolve()
    if output_root.exists():
        raise FileExistsError(f"refusing to overwrite {output_root}")
    ideal_global = yaml.safe_load(
        (args.global_root / "ideal_global.yaml").read_text(encoding="utf-8")
    )
    operating_global = yaml.safe_load(
        (args.global_root / "operating_global.yaml").read_text(encoding="utf-8")
    )
    if operating_global["VDD"]["default"] != 3.3 or operating_global["VSS"]["fixed_value"] != 0.0:
        raise ValueError("unexpected global operating defaults")
    configure_ngspice()
    records = []
    for index, source_id in enumerate(ORIGINAL_IDS, start=1):
        record = run_one(output_root, index, source_id, ideal_global)
        records.append(record)
        print(
            record["circuit_id"],
            f"OP={record['op_status']}",
            f"DC={record['dc_status']}",
            f"AC={record['ac_status']}",
            f"TRAN={record['transient_status']}",
            flush=True,
        )
    dump_yaml(output_root / "simulation_summary.yaml", {"circuits": records})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
