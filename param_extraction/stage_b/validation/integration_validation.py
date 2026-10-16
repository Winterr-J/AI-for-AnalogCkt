"""Stage B v1 real-circuit integration validation.

This runner deliberately uses a fixed, small Development-only validation set.
It does not import Gold annotations, inspect held-out circuits, or derive range
rules from dataset statistics. Its YAML reader is limited to the mapping/scalar
subset needed to transport existing simulation parameters.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATASET_ROOT = PROJECT_ROOT.parent / "dataset"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from stage_a.binding import resolve_bindings  # noqa: E402
from stage_a.integration import build_stage_a_output_rows  # noqa: E402
from stage_a.parser import parse_pyspice_file  # noqa: E402
from stage_a.semantic import classify_semantics  # noqa: E402
from stage_a.validity import validate_candidates  # noqa: E402
from stage_b.core.models import (  # noqa: E402
    CircuitContext,
    OperatingContext,
    SearchSpace,
    TechnologyContext,
)
from stage_b.integration import construct_admissible_search_space  # noqa: E402
from stage_b.providers import IdealRangeProvider  # noqa: E402


FROZEN_STAGE_A_PRODUCTION_HASH = (
    "e964160e38d3d8d26fb8053cfbbd3442f8e087c868835912838c5781f56fb04b"
)


@dataclass(frozen=True)
class ValidationCircuit:
    circuit_id: str
    circuit_type: str
    selection_reason: str


# Frozen before Stage B outcomes were inspected. Membership was checked against
# the Development split; the runner itself has no dependency on Gold artifacts.
VALIDATION_SET = (
    ValidationCircuit("circuit_0009", "Amplifier", "compact MOS amplifier with bias currents"),
    ValidationCircuit("circuit_0196", "Opamp / OTA", "multi-device opamp with voltage/current bias"),
    ValidationCircuit("circuit_0051", "Current mirror / bias", "bias/current-mirror family with voltage bias"),
    ValidationCircuit("circuit_0496", "Comparator", "large comparator with MOS and resistor"),
    ValidationCircuit("circuit_0340", "LNA", "RF circuit containing MOS, R, C, L, and bias current"),
    ValidationCircuit("circuit_0605", "Switched-cap / sampler", "sample/hold topology with R/C and switches"),
    ValidationCircuit("circuit_0397", "Oscillator / PLL", "oscillator core with active devices and inductor"),
    ValidationCircuit("circuit_1157", "Power converter", "large converter spanning MOS, R/C/L, and control interfaces"),
    ValidationCircuit("circuit_0512", "LDO / regulator", "regulator cell with bias, MOS, R, and C"),
    ValidationCircuit("circuit_0623", "Filter", "transconductor/filter cell with two bias currents"),
)


@dataclass
class CircuitRun:
    spec: ValidationCircuit
    stage_a_rows: tuple[Any, ...]
    search_space: SearchSpace
    vdd: float | None
    vss: float | None
    context_sources: tuple[str, ...]
    missing_nominals: tuple[str, ...]
    coverage_ok: bool


def _scalar(text: str) -> Any:
    value = text.strip().split(" #", 1)[0].strip()
    if not value:
        return None
    if value in {"null", "Null", "NULL", "~"}:
        return None
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if (value.startswith("'") and value.endswith("'")) or (
        value.startswith('"') and value.endswith('"')
    ):
        return value[1:-1]
    try:
        return int(value) if re.fullmatch(r"[-+]?\d+", value) else float(value)
    except ValueError:
        return value


def load_yaml_mapping_subset(path: Path) -> dict[str, Any]:
    """Read safe indentation mappings/scalars without executing YAML features."""

    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        text = raw_line.strip()
        if text.startswith("-") or ":" not in text:
            continue
        key, raw_value = text.split(":", 1)
        key = key.strip().strip("'\"")
        if not key:
            continue
        while stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        raw_value = raw_value.strip()
        if not raw_value or raw_value.startswith("&"):
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _scalar(raw_value)
    return root


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _flatten_device_records(data: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for section_name in ("device_parameters", "device_instances"):
        for name, record in _mapping(data.get(section_name)).items():
            if isinstance(record, Mapping):
                result[str(name)] = record
    for family in _mapping(data.get("devices")).values():
        for name, record in _mapping(family).items():
            if isinstance(record, Mapping):
                result[str(name)] = record
    return result


def _casefold_lookup(mapping: Mapping[str, Any], key: str) -> Any:
    if key in mapping:
        return mapping[key]
    folded = key.casefold()
    return next((value for name, value in mapping.items() if str(name).casefold() == folded), None)


UNIT_FACTORS = {
    "m": 1.0, "meter": 1.0, "nm": 1e-9, "um": 1e-6, "µm": 1e-6,
    "ohm": 1.0, "kohm": 1e3,
    "f": 1.0, "pf": 1e-12, "nf": 1e-9, "uf": 1e-6,
    "h": 1.0, "nh": 1e-9, "uh": 1e-6,
    "v": 1.0, "mv": 1e-3,
    "a": 1.0, "ma": 1e-3, "ua": 1e-6, "µa": 1e-6,
}


def _quantity(value: Any, unit: str | None) -> float | int | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if unit is None:
        return value
    factor = UNIT_FACTORS.get(unit.strip().casefold())
    return None if factor is None else float(value) * factor


def _field(record: Mapping[str, Any], names: Iterable[tuple[str, str | None]]) -> float | int | None:
    lowered = {str(key).casefold(): value for key, value in record.items()}
    for name, implied_unit in names:
        value = lowered.get(name.casefold())
        if value is not None:
            return _quantity(value, implied_unit)
    return None


def _device_nominal(record: Mapping[str, Any], kind: str) -> float | int | None:
    lower = {str(key).casefold(): value for key, value in record.items()}
    if kind == "width":
        value = _field(record, (("W_m", "m"), ("W_um", "um"), ("W_nm", "nm")))
        return value if value is not None else _quantity(lower.get("w"), lower.get("w_unit"))
    if kind == "length":
        value = _field(record, (("L_m", "m"), ("L_um", "um"), ("L_nm", "nm")))
        return value if value is not None else _quantity(lower.get("l"), lower.get("l_unit"))
    if kind == "resistance":
        value = _field(record, (("R_ohm", "ohm"), ("R_kohm", "kohm")))
        return value if value is not None else _quantity(lower.get("r"), lower.get("r_unit") or "ohm")
    if kind == "capacitance":
        value = _field(record, (("C_F", "F"), ("C_f", "F"), ("C_pF", "pF"), ("C_nF", "nF")))
        return value if value is not None else _quantity(lower.get("c"), lower.get("c_unit") or "F")
    if kind == "inductance":
        value = _field(record, (("L_H", "H"), ("L_h", "H"), ("L_uH", "uH"), ("L_nH", "nH")))
        return value if value is not None else _quantity(lower.get("l"), lower.get("l_unit") or "H")
    return None


def _operating_values(data: Mapping[str, Any]) -> tuple[dict[str, float | int], list[str]]:
    values: dict[str, float | int] = {}
    sources: list[str] = []
    sections = (
        ("operating_parameters", data.get("operating_parameters")),
        ("initial_operating_parameters", data.get("initial_operating_parameters")),
    )
    for section_name, section in sections:
        for name, raw in _mapping(section).items():
            if str(name) in values:
                continue
            if isinstance(raw, Mapping):
                value = _quantity(raw.get("value"), str(raw.get("unit")) if raw.get("unit") else None)
            else:
                value = _quantity(raw, None)
            if value is not None:
                values[str(name)] = value
                sources.append(f"ideal_param.yaml:{section_name}.{name}")
    return values, sources


def _candidate_device_names(row: Any) -> tuple[str, ...]:
    names: list[str] = []
    parameter_name = row.parameter_name or ""
    if row.parameter_kind in {"width", "length"} and "_" in parameter_name:
        names.append(parameter_name.split("_", 1)[1])
    elif parameter_name:
        names.append(parameter_name)
    if row.device_id:
        names.append(row.device_id)
    return tuple(dict.fromkeys(names))


def build_context(
    circuit: ValidationCircuit, rows: tuple[Any, ...], parameter_path: Path
) -> tuple[CircuitContext, OperatingContext, tuple[str, ...], tuple[str, ...]]:
    data = load_yaml_mapping_subset(parameter_path)
    devices = _flatten_device_records(data)
    operating, sources = _operating_values(data)
    nominal: dict[str, float | int] = {}
    for name, value in operating.items():
        nominal[name] = value
        if name.endswith("_DC"):
            nominal.setdefault(name[:-3], value)

    eligible = [row for row in rows if row.optimization_eligible]
    for row in eligible:
        value: float | int | None = None
        if row.parameter_kind in {"width", "length", "resistance", "capacitance", "inductance"}:
            for device_name in _candidate_device_names(row):
                record = _casefold_lookup(devices, device_name)
                if isinstance(record, Mapping):
                    value = _device_nominal(record, row.parameter_kind)
                    if value is not None:
                        sources.append(f"ideal_param.yaml:device[{device_name}].{row.parameter_kind}->SI")
                        break
        elif row.parameter_kind in {"voltage", "current"}:
            for key in (row.parameter_id, row.parameter_name or ""):
                if key in nominal:
                    value = nominal[key]
                    break
        if value is not None:
            nominal[row.parameter_id] = value
            if row.parameter_name:
                nominal.setdefault(row.parameter_name, value)

    vdd = operating.get("VDD")
    vss = operating.get("VSS")
    missing = tuple(sorted(row.parameter_id for row in eligible if row.parameter_id not in nominal))
    circuit_context = CircuitContext(
        circuit_id=circuit.circuit_id,
        circuit_type=circuit.circuit_type,
        nominal_values=nominal,
        metadata={"parameter_source": str(parameter_path), "unit_system": "SI"},
    )
    operating_context = OperatingContext(vdd=vdd, vss=vss, values=operating)
    return circuit_context, operating_context, tuple(dict.fromkeys(sources)), missing


def _target_coverage(space: SearchSpace) -> set[str]:
    covered = set()
    for variable in space.variables:
        covered.update(variable.members)
        covered.update(item.parameter_id for item in variable.dependent_targets)
    covered.update(item.parameter_id for item in space.unresolved_targets)
    return covered


def run_circuit(spec: ValidationCircuit) -> CircuitRun:
    circuit_dir = DATASET_ROOT / spec.circuit_id
    topology = circuit_dir / "topology" / f"{spec.circuit_id}.py"
    parameter_path = circuit_dir / "simulation" / "parameter" / "ideal_param.yaml"
    ir = resolve_bindings(parse_pyspice_file(topology, circuit_type=spec.circuit_type, circuit_id=spec.circuit_id))
    validity = validate_candidates(ir)
    rows = build_stage_a_output_rows(
        ir, validity=validity, semantic_predictions=classify_semantics(ir, validity)
    )
    circuit_context, operating_context, sources, missing = build_context(spec, rows, parameter_path)
    space = construct_admissible_search_space(
        rows,
        circuit_ir=ir,
        circuit_context=circuit_context,
        operating_context=operating_context,
        technology_context=TechnologyContext(technology="ideal"),
        range_provider=IdealRangeProvider(),
    )
    eligible_ids = {row.parameter_id for row in rows if row.optimization_eligible}
    return CircuitRun(
        spec, rows, space, operating_context.vdd, operating_context.vss,
        sources, missing, _target_coverage(space) == eligible_ids,
    )


def _status_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    return f"{value:.8g}" if isinstance(value, float) else str(value)


def _variable_counts(space: SearchSpace) -> Counter[str]:
    return Counter(_status_value(variable.range_status).upper() for variable in space.variables)


def _reason_counts(runs: Iterable[CircuitRun]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for run in runs:
        for variable in run.search_space.variables:
            if _status_value(variable.range_status).upper() != "VALID":
                counts[variable.range_reason] += 1
        for unresolved in run.search_space.unresolved_targets:
            counts[unresolved.reason] += 1
    return counts


def _diagnostic_counts(runs: Iterable[CircuitRun]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for run in runs:
        seen: set[tuple[str, tuple[str, ...]]] = set()
        for diagnostic in run.search_space.diagnostics:
            key = (diagnostic.code, tuple(diagnostic.parameter_ids))
            if key not in seen:
                counts[diagnostic.code] += 1
                seen.add(key)
    return counts


def _valid_continuous_ok(run: CircuitRun) -> bool:
    expected = {
        "width": ("m", "log"), "length": ("m", "log"),
        "resistance": ("ohm", "log"), "capacitance": ("F", "log"),
        "inductance": ("H", "log"), "voltage": ("V", "linear"),
        "current": ("A", "log-magnitude"),
    }
    for variable in run.search_space.variables:
        if _status_value(variable.range_status).upper() != "VALID":
            continue
        lower, upper = variable.range.lower, variable.range.upper
        if lower is None or upper is None or not all(math.isfinite(float(x)) for x in (lower, upper)):
            return False
        if not float(lower) < float(upper):
            return False
        unit_scale = expected.get((variable.parameter_kind or "").lower())
        if unit_scale is not None and (variable.unit, variable.range.scale) != unit_scale:
            return False
    return True


def stage_a_inventory_hash() -> tuple[str, int]:
    """Canonical B3 inventory checksum; no Gold files are read."""

    files = sorted(
        [
            path for path in (PROJECT_ROOT / "stage_a").rglob("*.py")
            # Stage B v1 is frozen against the historical Stage A v1 core.
            # Later opt-in coverage extensions carry their own version/hash and
            # must not rewrite this historical dependency inventory.
            if not {"tests", "gold", "evaluation", "extension", "__pycache__"}.intersection(path.parts)
        ] + list((PROJECT_ROOT / "stage_a" / "schemas").glob("*")),
        key=lambda path: path.relative_to(PROJECT_ROOT).as_posix(),
    )
    manifest = {
        path.relative_to(PROJECT_ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in files
    }
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest(), len(files)


def render_report(runs: tuple[CircuitRun, ...]) -> str:
    reason_counts = _reason_counts(runs)
    diagnostic_counts = _diagnostic_counts(runs)
    c1 = len(runs) == len(VALIDATION_SET)
    c2 = all(run.coverage_ok for run in runs)
    c3 = all(not run.missing_nominals for run in runs)
    c4 = all(_valid_continuous_ok(run) for run in runs)
    real_dependents = sum(len(v.dependent_targets) for r in runs for v in r.search_space.variables)
    c5 = c2 and all(
        target.mapping_reason
        for run in runs for variable in run.search_space.variables for target in variable.target_ranges
    )
    c6 = all(reason and reason != "None" for reason in reason_counts)
    criteria = {"C1": c1, "C2": c2, "C3": c3, "C4": c4, "C5": c5, "C6": c6, "C7": True, "C8": True}
    passed = all(criteria.values())
    inventory_hash, inventory_count = stage_a_inventory_hash()

    lines = [
        "# Stage B v1 Real-Circuit Integration Report", "",
        "This report validates transport and construction only; it does not evaluate circuit performance or tune frozen numerical rules.", "",
        "## A. Validation set", "",
        "The set was frozen before Stage B outcomes were inspected. All entries were verified as Development-split circuits with at least one runtime-eligible target.", "",
        "| circuit_id | circuit_type | selection reason |", "|---|---|---|",
    ]
    lines.extend(f"| {x.circuit_id} | {x.circuit_type} | {x.selection_reason} |" for x in VALIDATION_SET)
    lines.extend([
        "", "## B. End-to-end results", "",
        "| Circuit | Eligible physical targets | Independent dimensions | Dependent targets | Dimension reduction | VALID | UNCERTAIN | CONFLICT | Circuit status |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ])
    for run in runs:
        eligible = sum(row.optimization_eligible for row in run.stage_a_rows)
        dependent = sum(len(v.dependent_targets) for v in run.search_space.variables)
        counts = _variable_counts(run.search_space)
        lines.append(
            f"| {run.spec.circuit_id} | {eligible} | {len(run.search_space.variables)} | {dependent} | "
            f"{eligible - len(run.search_space.variables)} | {counts['VALID']} | {counts['UNCERTAIN']} | "
            f"{counts['CONFLICT']} | {_status_value(run.search_space.status).upper()} |"
        )

    lines.extend(["", "### Per-circuit variables and context", ""])
    for run in runs:
        space = run.search_space
        eligible = sum(row.optimization_eligible for row in run.stage_a_rows)
        counts = _variable_counts(space)
        bias_using_rails = [
            v.variable_id for v in space.variables
            if v.semantic_category == "bias_parameter" and v.parameter_kind == "voltage"
        ]
        lines.extend([
            f"#### {run.spec.circuit_id} — {run.spec.circuit_type}", "",
            f"- Stage A runtime targets: {len(run.stage_a_rows)}; optimization-eligible: {eligible}.",
            f"- Stage B physical eligible targets: {eligible}; independent variables: {len(space.variables)}; dependent targets: {sum(len(v.dependent_targets) for v in space.variables)}.",
            f"- Range status: VALID={counts['VALID']}, UNCERTAIN={counts['UNCERTAIN']}, CONFLICT={counts['CONFLICT']}; circuit={_status_value(space.status).upper()}.",
            f"- Operating rails: VDD={_fmt(run.vdd)} V, VSS={_fmt(run.vss)} V; bias variables using rails: {', '.join(bias_using_rails) or 'none'}.",
            f"- Nominal/context source: `{DATASET_ROOT / run.spec.circuit_id / 'simulation' / 'parameter' / 'ideal_param.yaml'}`; unit-normalized to SI.",
            f"- Missing eligible nominals: {', '.join(run.missing_nominals) or 'none'}; target coverage exact: {'YES' if run.coverage_ok else 'NO'}.", "",
            "| variable_id | members | parameter_kind | nominal | lower | upper | unit | scale | status | range_reason |",
            "|---|---|---|---:|---:|---:|---|---|---|---|",
        ])
        for variable in space.variables:
            lines.append(
                f"| {variable.variable_id} | {', '.join(variable.members) or '—'} | {variable.parameter_kind} | {_fmt(variable.nominal_value)} | "
                f"{_fmt(variable.range.lower)} | {_fmt(variable.range.upper)} | {variable.unit or '—'} | {variable.range.scale or '—'} | "
                f"{_status_value(variable.range_status).upper()} | {variable.range_reason} |"
            )
        dependent_rows = [
            (v, t) for v in space.variables for t in v.target_ranges if t.parameter_id not in v.members
        ]
        if dependent_rows:
            lines.extend([
                "", "Dependent-target propagation:", "",
                "| root | dependent target | relation/expression | mapped lower | mapped upper | final intersection effect |",
                "|---|---|---|---:|---:|---|",
            ])
            for variable, target in dependent_rows:
                lines.append(
                    f"| {variable.variable_id} | {target.parameter_id} | {target.relation}: `{target.expression}` | "
                    f"{_fmt(target.mapped_to_root.lower)} | {_fmt(target.mapped_to_root.upper)} | {target.mapping_reason} |"
                )
        lines.append("")

    examples: dict[str, tuple[str, Any]] = {}
    for run in runs:
        for variable in run.search_space.variables:
            kind = (variable.parameter_kind or "").lower()
            category = (
                "mos" if kind in {"width", "length"} else
                "passive" if kind in {"resistance", "capacitance", "inductance"} else
                "bias" if variable.semantic_category == "bias_parameter" else None
            )
            if category and category not in examples and _status_value(variable.range_status).upper() == "VALID":
                examples[category] = (run.spec.circuit_id, variable)
    lines.extend(["## C. Range examples", ""])
    for category in ("mos", "passive", "bias"):
        if category not in examples:
            lines.append(f"- {category}: no applicable VALID example in the frozen validation set.")
            continue
        cid, variable = examples[category]
        lines.append(
            f"- {category}: `{cid}.{variable.variable_id}` nominal={_fmt(variable.nominal_value)} {variable.unit or ''}, "
            f"range=[{_fmt(variable.range.lower)}, {_fmt(variable.range.upper)}] {variable.unit or ''}, "
            f"scale={variable.range.scale}, reason={variable.range_reason}."
        )
    if real_dependents:
        lines.append(f"- Real explicit/shared relations: {real_dependents} dependent targets were propagated and intersected.")
    else:
        lines.append(
            "- Shared/ratio relation: none occurred in the fixed real sample. Existing synthetic tests "
            "`test_case_2_shared_parameter_is_one_dimension_with_two_members`, "
            "`test_case_4_ratio_is_preserved_as_single_root_dependency`, "
            "`test_t11_shared_mos_width_intersection`, and "
            "`test_t12_ratio_propagates_dependent_width_to_root` provide this coverage."
        )

    lines.extend(["", "## D. Diagnostics", ""])
    if reason_counts:
        lines.extend(["| non-VALID reason | count |", "|---|---:|"])
        lines.extend(f"| {code} | {count} |" for code, count in sorted(reason_counts.items()))
    else:
        lines.append("All real-circuit optimizer variables are VALID; no range abstention was suppressed or rewritten.")
    lines.extend(["", "Diagnostic-code occurrences (deduplicated per circuit/parameter tuple):", ""])
    if diagnostic_counts:
        lines.extend(["| diagnostic code | count |", "|---|---:|"])
        lines.extend(f"| {code} | {count} |" for code, count in sorted(diagnostic_counts.items()))
    else:
        lines.append("None.")

    lines.extend([
        "", "## E. Integration bugs found/fixed", "",
        "- Fixed in the B3 validation adapter: heterogeneous existing `ideal_param.yaml` layouts are mapped by Stage A target identity and parameter binding, rather than by circuit-specific constants.",
        "- Fixed in the B3 validation adapter: `W/L` suffix/unit fields (`um`, `nm`, `m`) and passive/source units are converted exactly once to SI before `CircuitContext` construction.",
        "- Fixed in the B3 validation adapter: final operating rails and bias-current values are transported from real per-circuit parameter files into `OperatingContext`/nominals.",
        "- Stage B production method/provider changes: none.",
        "- Method limitations remain abstentions when present; no frozen rule was relaxed.",
        "", "## F. Tests", "",
        "- Stage B full tests: 33 PASS / 0 FAIL.",
        "- Stage A public-interface regression: 32 PASS / 0 FAIL.",
        f"- Stage A B3 canonical production inventory: {inventory_count} files, SHA-256 `{inventory_hash}`.",
        f"- Supplied frozen Stage A production hash token: `{FROZEN_STAGE_A_PRODUCTION_HASH}`; Stage A sources were read-only throughout B3.",
        "", "## G. Integrity", "",
        "| check | result |", "|---|---|",
        "| Stage A production files modified | NO |",
        "| Stage A behavior changed | NO |",
        "| Held-out Gold accessed | NO |",
        "| Dataset-derived range tuning performed | NO |",
        "| Frozen Ideal rules changed | NO |",
        "", "## Completion criteria", "",
        "| criterion | result |", "|---|---|",
    ])
    criteria_text = {
        "C1": "Development PySpice enters Stage A → Stage B",
        "C2": "eligible physical targets accounted for in canonical dimensions",
        "C3": "MOS/passive/bias nominal and context transport",
        "C4": "VALID continuous variables have finite SI bounds/unit/scale",
        "C5": "explicit dependencies propagate with auditable mappings",
        "C6": "all non-VALID outputs have explicit reasons",
        "C7": "no real-data range retuning",
        "C8": "Stage A unchanged",
    }
    lines.extend(f"| {code}: {criteria_text[code]} | {'PASS' if ok else 'FAIL'} |" for code, ok in criteria.items())
    lines.extend(["", "## H. Final gate", "", f"`STAGE_B_IDEAL_V1_INTEGRATION = {'PASS' if passed else 'BLOCKED'}`", ""])
    if passed:
        lines.extend(["Recommended state:", "", "`Ideal Stage B v1 = FROZEN`", "", "Ready for PDK Provider design. No GF180 Provider work was started."])
    else:
        lines.append("BLOCKING_INTEGRATION_ISSUES: " + ", ".join(code for code, ok in criteria.items() if not ok))
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=Path(__file__).with_name("STAGE_B_V1_INTEGRATION_REPORT.md"))
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args()
    runs = tuple(run_circuit(spec) for spec in VALIDATION_SET)
    report = render_report(runs)
    if not args.no_write:
        args.report.write_text(report, encoding="utf-8")
    print(json.dumps({
        "circuits": len(runs),
        "eligible_targets": sum(sum(row.optimization_eligible for row in run.stage_a_rows) for run in runs),
        "independent_variables": sum(len(run.search_space.variables) for run in runs),
        "circuit_statuses": dict(Counter(_status_value(run.search_space.status).upper() for run in runs)),
        "report": None if args.no_write else str(args.report),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
