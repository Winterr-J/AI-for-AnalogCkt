# CM0469 Independent D0 V2 — Batch input mapping

Origin: `/home/ubuntu/cm0469_d0_runs/independent_d0_v2_93a6a79bac93`.
The original `run_manifest.json` stays at that location, including its absolute
paths; it is not copied into the Batch input package. Its SHA-256 at packaging
was `67706b312bb4cd9252723273e1dc81fda1e9928d2e97b2b56e9e4d05a8380def`.
The producer Git revision was
`93a6a79bac93f517e275a9dadb7011809a37a08f`.

| Original path within run | Packaged path within this directory | SHA-256 |
| --- | --- | --- |
| `active_learning_config.yaml` | `active_learning_config.yaml` | `eba11c5a828e5551cb7e5adf6d2cce41ec973c6bd3b0ada8ac8181a8aabb7dba` |
| `d0/d0_v1_seed42.jsonl` | `d0/d0_v1_seed42.jsonl` | `b765746d38b5bc2adc9ecaf1b76b63a9e57de64d1c8b2fb4d914097fe3e85da3` |
| `d0/d0_v1_seed42.csv` | `d0/d0_v1_seed42.csv` | `3b4052d04f388a42c1134dbe3261c988d977541b630a99907ca84d361a7439e8` |
| `d0/d0_v1_seed42_initialization.json` | `d0/d0_v1_seed42_initialization.json` | `2fedd82e107be35de7b1fe86cf318c275d92e50106f563225340103bc6c4393b` |

The config and all three D0 artifacts are byte-for-byte copies. The config
already resolves `d0/d0_v1_seed42.jsonl` relative to itself; no path edit was
needed. Its `initialize_if_missing: true` is required by the current
independent-D0 config contract. The Batch production child instead enforces
`require_existing_d0=True` and pins the config and D0 hashes. Do not use the
ordinary AL CLI as a frozen-D0 production launcher.

`job.json` is an exact B2A job fragment, not a runnable or authorized batch
manifest. After the reviewed commit is frozen, build the complete batch manifest
at the repository root using the final Git revision and raw-byte source closure.
Operator-selected model, credential scope, request cap, worker timeout, retry
policy, batch ID and separate approval are still required. This package neither
creates nor consumes a production authorization.

## Final Source Closure gate

Use the `source_files` and `runtime_assets` path sets in
`ActiveLearning/docs/batch_runner_v2/BATCH_RUNNER_V2_B2A_SOURCE_CLOSURE.json`
as a starting list. Exclude `generate_closure.py` (an absent Windows
packaging helper), the old formal `active_learning_config.yaml`, and the
old `d0_v3_seed42.jsonl`. Include the new production launcher and all four
byte-preserved inputs listed above. The remaining 120 baseline paths were
checked in this Ubuntu checkout against `git show 93a6a79:<path>` with exact
byte equality; no LF/CRLF normalization was used.

This is a pre-freeze path recipe, **not** the final closure. After an operator
commits the reviewed package, create a separate clean checkout at that final
SHA, compute every closure digest from `git show <final-SHA>:<path>`, and
require each checkout file to have the same raw-byte digest. Only then write
`source_revision`, `source_closure`, and the exact `job.json` entry into a
Batch manifest placed at the repository root. Do not put an unknown future
commit SHA into the commit that creates it. Run read-only `plan` before any
independently approved production launch.
