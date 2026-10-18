"""Explicit production entry point for the existing authorized V2 scheduler."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .runner import run_authorized_batch


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Authorized Batch Runner V2 production launcher")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--authorization-sha256", required=True)
    parser.add_argument("--enable-production", action="store_true")
    args = parser.parse_args(argv)
    if not args.enable_production:
        parser.error("production requires --enable-production")
    if os.environ.get("BATCH_RUNNER_V2_PRODUCTION_AUTHORIZATION_SHA256") != args.authorization_sha256:
        parser.error("production authorization SHA-256 is not pinned in the environment")
    result = run_authorized_batch(
        args.manifest,
        args.output_root,
        args.authorization,
        args.authorization_sha256,
        enable_production=True,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
