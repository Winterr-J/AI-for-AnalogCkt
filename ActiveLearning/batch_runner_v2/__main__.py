"""Read-only and synthetic-mock CLI; deliberately no real execute command."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .runner import plan_batch, report_batch, run_mock_batch, status_batch


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Offline-only AL Batch Runner V2-A")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "mock-run"):
        command = sub.add_parser(name)
        command.add_argument("--manifest", type=Path, required=True)
        if name == "mock-run":
            command.add_argument("--output-root", type=Path, required=True)
            command.add_argument("--fixtures", type=Path, required=True)
    for name in ("status", "report"):
        command = sub.add_parser(name)
        command.add_argument("--batch-id", required=True)
        command.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "plan":
        result = plan_batch(args.manifest)
    elif args.command == "mock-run":
        fixtures = json.loads(args.fixtures.read_text(encoding="utf-8"))
        if not isinstance(fixtures, dict):
            parser.error("fixtures must be a job-id to outcome mapping")

        def synthetic(job: dict, _attempt: Path) -> dict:
            result = fixtures.get(job["job_id"])
            if not isinstance(result, dict):
                raise ValueError("missing synthetic fixture")
            if result.get("raise"):
                return {"state": "FAILED_TERMINAL", "error_code": "SYNTHETIC_TERMINAL"}
            return result

        result = run_mock_batch(args.manifest, args.output_root, synthetic)
    elif args.command == "status":
        result = status_batch(args.batch_id, args.output_root)
    else:
        result = report_batch(args.batch_id, args.output_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
