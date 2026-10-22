"""Guarded, sequential Batch 012 Qwen AL recovery. Dry-run by default."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ORDER = ("1673", "1674", "1677", "1680", "1671")
LIMITS = {f"circuit_{number}": (225 if number == "1671" else 240) for number in ORDER}
NEW_BATCH_CAP = 1185
HISTORICAL_1671_REQUESTS = 15
ORIGINAL_TOTAL_CAP = 1200
MODEL = "qwen-flash-2025-07-28"
ENDPOINT = "https://ws-hgxzyx31pw18igy0.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
PREP = Path("/home/ubuntu/batch012_qwen_manual_preparation_v2")
OLD_LEDGER = Path("/home/ubuntu/batch012_qwen_remaining5_v1/circuit_1671/physical_attempts.jsonl")
OLD_LEDGER_SHA256 = "38e4ddfad867ee4c617feff687543be75ab2560ac3bb3129ff1a1bb9832a0cfa"
ROOT = Path(__file__).resolve().parents[1]

# These are the already prepared, byte-frozen independent D0 inputs, not AL history.
INPUTS = {
    "circuit_1673": ("OpAmp_circuit_1673_pfykmbb_",
                     "8af24ac1ef1c150bc824297d2115c4f1c73279e9404008f2b546910a305d0c7a",
                     "04a15d8d8837000dcb653da78caa64f9d049fe892ddc1e2f07e1d63f0c98270b"),
    "circuit_1674": ("OpAmp_circuit_1674_hqeth90a",
                     "ae826ec35f61b3ea4f7a4af5f68645be47d911f412f2952df5b9ad08527c0444",
                     "7ab1140bf942e5069b945903808ffbea1f23ab9714d1d28cfdc502200fabfcbe"),
    "circuit_1677": ("OpAmp_circuit_1677_c5bqhtks",
                     "885e32863bb99a38ca43b0cf08e7aaac909dac2007ee0a3db920d6cc47ff048d",
                     "530a69b2744ab7411502fe7e51b163f1dd9235dd9328d13faa4829e123778760"),
    "circuit_1680": ("OpAmp_circuit_1680_nr8ra556",
                     "e5cda81e7736609477afb23aa16cb7717fad425c914dbc2f0af3b4d164fdb9e7",
                     "3900f0c43c402f33d02f822abdd63997a331d5083f17e27ba02f71fc247d3c67"),
    "circuit_1671": ("OpAmp_circuit_1671_9waw8iq9",
                     "730094cbf0cb6d81fe22b577c972c49c392068f780cf1fda1acf99621fa12bb9",
                     "36a10a14fe5e45792a98dcf6e7c372f135875e7004455911b95842b8d37e2e52"),
}
DATASET_FILES = (
    "metadata.txt",
    "parameters/ideal_design_space.yaml",
    "parameters/operating_space.yaml",
    "parameters/tunable_parameters.yaml",
    "simulation/parameter/ideal_param.yaml",
    "simulation/results/ideal/op_results.yaml",
    "topology/{cid}.cir",
    "topology/{cid}.py",
)
SOURCE_PREFIXES = ("ActiveLearning/", "run/", "conversion/", "simulation_metrics/")


class RecoveryError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_new(path: Path, payload: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def tracked_sources(root: Path) -> list[str]:
    names = subprocess.check_output(
        ["git", "-C", str(root), "ls-files", "-z"], text=False
    ).split(b"\0")
    return sorted(
        name.decode("utf-8") for name in names if name
        and name.decode("utf-8").startswith(SOURCE_PREFIXES)
    )


def expected_assets(root: Path) -> list[Path]:
    paths = [OLD_LEDGER]
    for cid in INPUTS:
        folder, _, _ = INPUTS[cid]
        base = PREP / cid / folder
        d0 = base / "d0/d0_v4_seed42.jsonl"
        paths.extend((PREP / cid / "result.json", base / "active_learning_config.yaml",
                      d0, d0.with_suffix(".csv"),
                      d0.with_name(d0.stem + "_initialization.json")))
        for name in DATASET_FILES:
            paths.append(root / "dataset" / cid / name.format(cid=cid))
    return sorted(paths, key=str)


def inspect(closure_path: Path, expected_closure_sha256: str) -> dict:
    if not expected_closure_sha256 or len(expected_closure_sha256) != 64:
        raise RecoveryError("exact source closure SHA-256 required")
    if sha256(closure_path) != expected_closure_sha256:
        raise RecoveryError("source closure file hash mismatch")
    closure = json.loads(closure_path.read_text(encoding="utf-8"))
    if closure.get("schema") != "batch012_qwen_al_recovery_v2_closure_v1":
        raise RecoveryError("unsupported source closure schema")
    revision = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
    ).strip()
    if closure.get("source_revision") != revision:
        raise RecoveryError("release HEAD differs from source closure")
    expected_root = Path(f"/home/ubuntu/AG_simulation_batch012_recovery_v2_release_{revision[:12]}")
    if ROOT != expected_root or Path(closure.get("release_root", "")).resolve() != ROOT:
        raise RecoveryError("not running from independent frozen Release worktree")
    if subprocess.check_output(
        ["git", "-C", str(ROOT), "status", "--porcelain"], text=True
    ).strip():
        raise RecoveryError("release worktree is not clean")
    entries = closure.get("source_files")
    names = tracked_sources(ROOT)
    if (not isinstance(entries, list) or
            [item.get("path") for item in entries] != names):
        raise RecoveryError("source closure does not cover exact tracked runtime scope")
    for item in entries:
        path = (ROOT / item["path"]).resolve()
        if not path.is_relative_to(ROOT) or sha256(path) != item.get("sha256"):
            raise RecoveryError(f"source byte mismatch: {item['path']}")
    assets = closure.get("input_assets")
    expected = expected_assets(ROOT)
    if (not isinstance(assets, list) or
            [item.get("path") for item in assets] != [str(path) for path in expected]):
        raise RecoveryError("input asset closure incomplete or reordered")
    for item, path in zip(assets, expected):
        if not path.is_file() or sha256(path) != item.get("sha256"):
            raise RecoveryError(f"input asset hash mismatch: {path}")
    if sha256(OLD_LEDGER) != OLD_LEDGER_SHA256:
        raise RecoveryError("historical 1671 request ledger changed")
    prior = [json.loads(line) for line in OLD_LEDGER.read_text().splitlines() if line.strip()]
    reserved = [row for row in prior if row.get("event") == "reserved"]
    if (len(reserved) != HISTORICAL_1671_REQUESTS or
            sorted(row.get("attempt_number") for row in reserved)
            != list(range(1, HISTORICAL_1671_REQUESTS + 1))):
        raise RecoveryError("historical 1671 request count is not the frozen 15")
    import yaml
    jobs = []
    for number in ORDER:
        cid = f"circuit_{number}"
        folder, config_hash, d0_hash = INPUTS[cid]
        base = PREP / cid / folder
        config_path = base / "active_learning_config.yaml"
        d0_path = base / "d0/d0_v4_seed42.jsonl"
        result = json.loads((PREP / cid / "result.json").read_text())
        if (result.get("status") != "PREPARATION_READY" or
                result.get("meets_spec_count") != 0 or
                Path(result.get("config_path", "")).resolve() != config_path or
                Path(result.get("d0_path", "")).resolve() != d0_path or
                sha256(config_path) != config_hash or sha256(d0_path) != d0_hash):
            raise RecoveryError(f"{cid}: frozen Config/D0 or preparation status differs")
        config = yaml.safe_load(config_path.read_text())
        data = config.get("data", {})
        settings = config.get("active_learning", {})
        if (settings.get("max_iterations") != 5 or
                data.get("d0_mode") != "independent" or
                data.get("d0_path_relative_to") != "config" or
                data.get("seed") != 42 or
                not isinstance(data.get("initial_anchor"), dict) or
                not (base / data.get("d0_path", "")).resolve() == d0_path or
                not isinstance(config.get("target_specs"), dict)):
            raise RecoveryError(f"{cid}: AL/D0 configuration contract differs")
        manifest = json.loads(
            d0_path.with_name(d0_path.stem + "_initialization.json").read_text()
        )
        if (manifest.get("d0_mode") != "independent" or
                manifest.get("seed") != 42 or
                manifest.get("anchor_sample_id") !=
                str(data["initial_anchor"].get("sample_id"))):
            raise RecoveryError(f"{cid}: independent D0 provenance differs")
        rows = [json.loads(line) for line in d0_path.read_text().splitlines() if line.strip()]
        if len(rows) != 20:
            raise RecoveryError(f"{cid}: expected 20 independent D0 rows")
        jobs.append({"circuit_id": cid, "config": config_path, "d0": d0_path,
                     "config_sha256": config_hash, "d0_sha256": d0_hash,
                     "records": len(rows), "new_request_cap": LIMITS[cid]})
    output_root = Path(f"/home/ubuntu/batch012_qwen_remaining5_recovery_v2_{revision[:12]}")
    if output_root.exists():
        raise RecoveryError("new batch output root already exists; no overwrite or replay")
    return {"revision": revision, "release_root": str(ROOT),
            "closure_sha256": expected_closure_sha256, "closure_source_files": len(entries),
            "closure_input_assets": len(assets), "jobs": jobs,
            "order": [job["circuit_id"] for job in jobs],
            "historical_1671_requests": HISTORICAL_1671_REQUESTS,
            "remaining_global_cap": NEW_BATCH_CAP,
            "output_root": str(output_root),
            "provider": "qwen", "model": MODEL, "credential_present": bool(os.getenv("DASHSCOPE_API_KEY"))}


class RequestLedger:
    """Append-only reservation before each physical request; never auto-resume."""

    def __init__(self, path: Path, historical_1671: int = HISTORICAL_1671_REQUESTS):
        self.path = path
        self.historical_1671 = historical_1671
        self.total_new = 0
        self.by_circuit = {cid: 0 for cid in LIMITS}
        with path.open("x", encoding="utf-8") as stream:
            stream.flush()
            os.fsync(stream.fileno())

    def _append(self, item: dict) -> None:
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(item, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def reserve(self, circuit_id: str) -> int:
        if circuit_id not in LIMITS:
            raise RecoveryError("unapproved circuit")
        if (self.by_circuit[circuit_id] >= LIMITS[circuit_id] or
                self.total_new >= NEW_BATCH_CAP or
                self.historical_1671 + self.total_new >= ORIGINAL_TOTAL_CAP):
            raise RecoveryError("PHYSICAL_REQUEST_BUDGET_EXHAUSTED")
        local = self.by_circuit[circuit_id] + 1
        global_number = self.total_new + 1
        self._append({"timestamp": now(), "event": "reserved",
                      "circuit_id": circuit_id, "attempt_number": local,
                      "global_new_attempt_number": global_number})
        self.by_circuit[circuit_id] = local
        self.total_new = global_number
        return local

    def finish(self, circuit_id: str, attempt_number: int, outcome: str,
               error_type: str | None = None) -> None:
        if outcome not in {"succeeded", "failed"}:
            raise RecoveryError("invalid attempt outcome")
        self._append({"timestamp": now(), "event": outcome,
                      "circuit_id": circuit_id, "attempt_number": attempt_number,
                      **({"error_type": error_type} if error_type else {})})


def execute(plan: dict) -> None:
    if (os.getenv("AL_LLM_PROVIDER") != "qwen" or
            os.getenv("QWEN_MODEL") != MODEL or
            os.getenv("QWEN_BASE_URL") != ENDPOINT or
            not os.getenv("DASHSCOPE_API_KEY")):
        raise RecoveryError("Qwen execution environment is not explicitly configured")
    from ActiveLearning.main import run_configured_active_learning
    from ActiveLearning.inverse_design.llm_client import call_inverse_design_llm
    root = Path(plan["output_root"])
    root.mkdir(mode=0o700, exist_ok=False)
    ledger = RequestLedger(root / "physical_attempts.jsonl")
    write_new(root / "release_binding.json", {
        "source_revision": plan["revision"],
        "source_closure_sha256": plan["closure_sha256"],
        "historical_1671_ledger": str(OLD_LEDGER),
        "historical_1671_ledger_sha256": OLD_LEDGER_SHA256,
        "historical_1671_requests": HISTORICAL_1671_REQUESTS,
        "new_request_cap": NEW_BATCH_CAP, "order": plan["order"],
        "started_at": now(),
    })
    for job in plan["jobs"]:
        cid = job["circuit_id"]
        job_root = root / cid
        job_root.mkdir(mode=0o700, exist_ok=False)
        write_new(job_root / "experiment_manifest.json", {
            "circuit_id": cid, "provider": "qwen", "model": MODEL,
            "source_revision": plan["revision"],
            "source_closure_sha256": plan["closure_sha256"],
            "config_path": str(job["config"]), "config_sha256": job["config_sha256"],
            "d0_path": str(job["d0"]), "d0_sha256": job["d0_sha256"],
            "max_iterations": 5, "new_request_cap": LIMITS[cid],
            "historical_1671_requests": HISTORICAL_1671_REQUESTS if cid == "circuit_1671" else 0,
            "started_at": now(),
        })

        def counted_client(prompt, **kwargs):
            for retry in range(3):
                number = ledger.reserve(cid)
                try:
                    response = call_inverse_design_llm(
                        prompt, provider="qwen", model=MODEL,
                        request_timeout=120.0, max_attempts=1,
                        usage_log=kwargs.get("usage_log"),
                        usage_context=kwargs.get("usage_context"),
                    )
                except Exception as error:
                    ledger.finish(cid, number, "failed", type(error).__name__)
                    if type(error).__name__ == "RetryExhaustedError" and retry < 2:
                        time.sleep(2 ** (retry + 1))
                        continue
                    raise
                ledger.finish(cid, number, "succeeded")
                return response
            raise RecoveryError("bounded logical retry exhausted")

        try:
            result = run_configured_active_learning(
                "OpAmp", cid, config_path=job["config"],
                llm_client=counted_client, runs_root=job_root / "runs",
                require_existing_d0=True,
                expected_config_sha256=job["config_sha256"],
                expected_d0_sha256=job["d0_sha256"],
            )
        except BaseException as error:
            write_new(job_root / "result.json", {
                "status": "FAILED_OR_INTERRUPTED",
                "error_type": type(error).__name__,
                "physical_attempts_reserved": ledger.by_circuit[cid],
                "finished_at": now(),
            })
            raise
        write_new(job_root / "result.json", {
            "status": "COMPLETED",
            "physical_attempts_reserved": ledger.by_circuit[cid],
            "iterations_completed": result.get("iterations_completed"),
            "stop_reason": result.get("stop_reason"),
            "finished_at": now(),
        })


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Batch 012 Qwen recovery V2; offline by default")
    parser.add_argument("--source-closure", required=True, type=Path)
    parser.add_argument("--expected-closure-sha256", required=True)
    parser.add_argument("--execute-real-al", action="store_true")
    args = parser.parse_args(argv)
    plan = inspect(args.source_closure, args.expected_closure_sha256)
    if not args.execute_real_al:
        print(json.dumps({
            "mode": "OFFLINE_PLAN", "revision": plan["revision"],
            "order": plan["order"], "d0_records": [job["records"] for job in plan["jobs"]],
            "source_files": plan["closure_source_files"],
            "input_assets": plan["closure_input_assets"],
            "historical_1671_requests": plan["historical_1671_requests"],
            "new_request_cap": plan["remaining_global_cap"],
            "output_root_absent": not Path(plan["output_root"]).exists(),
            "credential_present": plan["credential_present"],
        }, indent=2))
        return 0
    execute(plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
