# Batch Runner V2 Git 发布审计（只读，2026-10-17）

结论：**V2_GIT_RELEASE_AUDIT_BLOCKED**。本报告是唯一新增文件；未暂存、提交、推送、部署或运行真实 LLM/SPICE/AL。当前工作树的离线测试结果不能替代“同一固定提交”的证明。B0 readiness 的 GO 仅允许隔离 Ubuntu 离线审查，不是生产批次授权。

## 1. Git 身份与发布阻断

| 项目 | 只读证据 |
|---|---|
| 本地分支/HEAD | `batch-runner-v2`，`86dbc2a83d78c3c88bccf3667eed7cd40afcafb1`，跟踪 `origin/batch-runner-v2`；提交标题为 “Add offline Batch Runner V2 and AL integration”。`main` 与 `dev` 均在 `bcd5b48`。 |
| 本地 remote | `origin` 的 fetch/push 均为 `https://github.com/Winterr-J/AI-for-AnalogCkt.git`。 |
| Ubuntu 所克隆仓库 | 操作员报告为 `Winterr-J/AG_simulation_deploy` 的 `main`；B1 记录旧部署 HEAD `274196a33dd8b348b8d371f8d8ff4163076b644f`。这是**另一个 GitHub 仓库**，不能假定从其 `origin` 能 fetch 本地分支。 |
| 工作树 | 21 个 tracked 文件已修改，另有大量 untracked 文件。`git ls-tree HEAD ActiveLearning/batch_runner_v2` 为空；V2 包的 9 个 `.py` 全部未跟踪。提交标题不能作为代码已发布的证据。 |

先由操作员确定唯一的目标 GitHub 仓库、可写权限及 Ubuntu 隔离检出路径，再在后续写入轮次建立发布分支/remote。**不得**在旧部署目录 `git switch`、`pull`、`reset` 或 `clean`；也不得在这次审计中修改本地 remote。若目标是 `AG_simulation_deploy`，需要人工确认其 history 与此仓库如何关联；不能直接假设可快进或推送同名分支。

## 2. B0 所需依赖闭包与分类

`BATCH_RUNNER_V2_B0_READINESS.md` 要求固定 SHA 的 V2 + 当前 AL 核心、隔离虚拟环境、离线假子进程和回归。静态调用链为 `batch_runner_v2/{schema,runner,storage}` → mock worker；A2 的 `al_process` → fake child，`llm_bridge` → tracked `ActiveLearning/inverse_design/llm_client.py` → `gemini_recovery`。未来真实 AL 边界为 `main.run_configured_active_learning` → `active_learning.run_active_learning` → onboarding/D0/逆向设计/预测/采集/evaluator → `run/single_stage_amplifier`、`simulation_metrics`、Stage A/B。V2 当前 CLI 只有 `plan/mock-run/status/report`，**没有获授权的真实批次执行入口**。

| 类别 | 精确文件或边界 | 发布判断 |
|---|---|---|
| 必需新代码（V2 离线 mock） | `ActiveLearning/batch_runner_v2/__init__.py`, `__main__.py`, `al_process.py`, `fake_al_child.py`, `gemini_recovery.py`, `llm_bridge.py`, `runner.py`, `schema.py`, `storage.py` | 9 个文件均 untracked，需在同一待发布提交纳入。 |
| 必需修改代码（AL 接口契约） | `ActiveLearning/main.py`, `ActiveLearning/active_learning.py`, `ActiveLearning/experiment_logging.py` | 当前均 modified。`main.py` 的 `runs_root`、`run_context_callback`、`require_existing_d0` 和配置/D0 SHA 参数属于 V2 集成契约；但三个文件还含其他电路变更，**不能未经逐 hunk 审查整体暂存**。 |
| 间接运行代码 | 现有 tracked 的 `ActiveLearning/inverse_design/llm_client.py`、`ActiveLearning/onboarding/`、`InitializeD0/`、`circuit_types/`、`inverse_design/`、`performance_prediction/`、`data_expansion/`、`real_evaluation/`、`validity/`、`run/single_stage_amplifier/run_ideal_dataset_simulations.py`、`simulation_metrics/`、`param_extraction/stage_a/` 与 `stage_b/` | 多数在 HEAD 已 tracked；不能只复制 V2 子目录。当前多处 modified，且新 circuit provider/evaluator 文件 untracked。要对明确的 B1 测试集合在干净固定 SHA 上验证导入闭包，不能由当前脏工作树推断全部闭合。 |
| 必需新离线测试 | `tests/test_batch_runner_v2.py`, `tests/test_batch_runner_v2_recovery.py`, `tests/test_batch_runner_v2_a2.py` | 三者使用临时合成配置、source、D0 和 fake transport/child，不需要真实电路数据。 |
| AL 集成回归测试 | tracked 且 modified 的 `tests/test_active_learning.py`；untracked 的 `tests/test_main.py`, `tests/test_experiment_logging.py`, `tests/test_inverse_designer.py` | B0 建议的同 SHA 回归集合；测试源本身也须同提交。Book625/AG626 pytest 文件是扩展回归，依赖各自冻结资产，应另定范围。 |
| 必需文档 | `ActiveLearning/docs/batch_runner_v2/BATCH_RUNNER_V2_B0_READINESS.md` 与本报告 | B0 为边界依据；A/A2 设计文档和 B1 历史记录可在逐项安全审查后附加，但不是 mock 运行依赖。 |
| 测试环境依赖 | tracked `requirements-server.txt` 列 `PySpice`, `numpy`, `scipy`, `PyYAML`, `openai`；B0 的 pytest 扩展回归另需 `pytest` | 现有 requirements 未锁定版本，也未列 pytest。应在 Ubuntu 独立 venv 核验版本/import、ngspice 命令与共享库；不因本审计修改依赖文件。 |
| 单独传输的运行资产 | **仅在具体批准的 job 清单确定后**逐件列出的 topology/source、参数/模型、配置、独立 D0、Descriptor 与 PDK/仿真依赖；保留原哈希并在隔离 release 内校验 | V2 schema 要求配置/D0/source SHA，且解析后的资产必须留在 manifest 父目录。不得用指向旧 checkout 的 symlink 绕过。现在没有生产 manifest，故不能给出完整生产数据文件清单；B1 synthetic mock 不需要这些资产。 |
| 不上传 | `.env`/`.env.*`、API key/私钥/证书、`*.pem`/`*.key`、一次性 `al_execution_approval.yaml`/`al_execution_claim.json`/`d0_execution_approval.yaml`、历史 `ActiveLearning/runs/`、Profiling/D0 结果、`*.raw`、服务器备份、`deployment/` 下载文件、批次状态/账本、大型实验数据、无关 MotorDriver `.asc` 修改 | approval/claim 与历史数据不是通用 V2 授权或 Git 发布材料。冻结 D0 若某个后续**具体批准**的离线测试必须使用，应走哈希化隔离资产流程，不随宽泛 `git add` 上传。 |

Book625/AG626 的 `ActiveLearning/tests/test_book625_al_m4.py`、`test_book625_local_al5_config.py`、`test_ag626_al_m7a.py` 以及 `book625_target.py`、`ag626_al.py`、0490 一次性授权与冻结 D0 均未纳入上述最小 mock 提交。若 B1 要求这些扩展回归，必须先建立逐文件独立清单、审批边界与临时 claim 测试策略。不能为了使测试通过而提交已消费 claim 或放宽保护。

## 3. `.gitignore` 与敏感性审查

根规则 `/*` 后对 `ActiveLearning/**` 作宽泛反向放行；当前 `.gitignore` 的未提交变更又额外放行 Comparator/VoltageReference 配置、0490 D0 approval 与 0489 D0 CSV/JSONL。因此 `git check-ignore -v --no-index` 显示 V2 代码/文档、`al_execution_approval.yaml`、`al_execution_claim.json` 和 MotorDriver `.raw` 均会被 `!/ActiveLearning/**` 放行。`*.raw` 没有通用拒绝规则。`.env`、`.env.*`、`*.pem`、`*.key`、`*.p12`、`*.pfx` 已显式忽略，但忽略规则不是秘密扫描，也不能保证被强制暂存的内容安全。**禁止** `git add .`、`git add -A` 或整目录 `git add ActiveLearning`。

在本轮指定的 V2 包、三组 V2 测试、V2 文档、`main.py` 和 `active_learning.py` 中，用私钥头、典型 Gemini/OpenAI key 形态及直接 `GEMINI_API_KEY` 赋值模式做了只读文件名扫描，未命中；这只是有限模式检查，不是完整凭据审计。发布前仍须对**实际 staged diff** 单独扫描并人工复核，尤其检查 prompt/日志中的数据、绝对路径与密钥；不得打印或上传命中的值。当前 `.gitignore` 整体修改不属于最小 V2 mock 交付，需单独审查并收紧 `.raw`、claim/approval 和输出排除后才考虑暂存。

对工作区作仅按路径名的补充枚举（排除 `.git` 与本地 `_codex_test_deps`）发现 2 个 `.raw`、2 个名称含 `approval` 的文件和 22 个名称含 `claim` 的文件；未发现工作区内 `.env`、`.pem`、`.key` 或名称含 `backup`/`.bak` 的文件。这只说明所用命名模式下的可见文件，不保证其他命名的备份、凭据或大数据不存在；发布仍须逐个审查确切暂存文件。

## 4. 本地离线核验与同提交结论

本轮使用 bundled Python 3.12，`PYTHONDONTWRITEBYTECODE=1`，没有真实 API/SPICE/AL：

| 测试 | 本轮结果 |
|---|---|
| `tests.test_batch_runner_v2` + `tests.test_batch_runner_v2_recovery` | 31 tests，PASS。 |
| `tests.test_batch_runner_v2_a2` 单独运行 | 15 tests，PASS。 |
| 上述三组一次合跑 | 46 tests 中 1 failure、1 error：A2 使用 0.2 s 假子进程 timeout，`success` 子进程有一次未及时产出 `result.json`，下游状态不符合断言；单独重跑全过。**记录为时序不稳定，不能宣称合跑稳定通过**。 |
| `tests.test_experiment_logging`, `tests.test_active_learning`, `tests.test_main`, `tests.test_inverse_designer` | 首次未设置本地测试依赖路径，因缺 `yaml` 导入而有 2 errors；加入已有 `_codex_test_deps` 到 `PYTHONPATH` 后 51 tests，PASS。该目录是本机测试环境，不是应提交的源码。 |

以上只验证当前混合工作树，未运行干净提交测试，也未在 Ubuntu 验证 Linux 进程组。当前 HEAD 缺 V2 包，故“AL 核心与 V2 同一提交版本可完成离线测试”**尚未成立**。发布前须从候选完整 SHA 建立隔离 worktree，确认 `git status --porcelain` 清洁、`git ls-tree` 包含所需文件，再在该 SHA 与独立 venv 上重跑上述测试；A2 合跑时序失败须复核并明确记录，不能掩盖。不能用当前工作树或旧 Ubuntu checkout 代测。

## 5. 后续安全分支及精确暂存方案（命令仅供人工后续使用；本轮未执行）

先确认目标仓库及可达 history；以当前 `batch-runner-v2` 分支为候选，不在旧部署上切分支。逐文件审查 `git diff`、untracked 源码、秘密扫描和测试后，推荐在独立干净 worktree 或经批准的后续工作轮次构造一个**固定 SHA**。下列第一组是最小 V2 mock 层的精确文件路径，绝不使用目录或通配符；在当前工作树直接执行它也只会暂存 mock 层，**不能**声称 AL 集成完整：

```powershell
git add -- ActiveLearning/batch_runner_v2/__init__.py ActiveLearning/batch_runner_v2/__main__.py ActiveLearning/batch_runner_v2/al_process.py ActiveLearning/batch_runner_v2/fake_al_child.py ActiveLearning/batch_runner_v2/gemini_recovery.py ActiveLearning/batch_runner_v2/llm_bridge.py ActiveLearning/batch_runner_v2/runner.py ActiveLearning/batch_runner_v2/schema.py ActiveLearning/batch_runner_v2/storage.py
git add -- tests/test_batch_runner_v2.py tests/test_batch_runner_v2_recovery.py tests/test_batch_runner_v2_a2.py
git add -- ActiveLearning/docs/batch_runner_v2/BATCH_RUNNER_V2_B0_READINESS.md ActiveLearning/docs/batch_runner_v2/V2_GIT_RELEASE_AUDIT.md
```

第二组是 B0 的 AL 集成回归候选文件；**必须先逐 hunk 确认混入的 Book625、AG626、Comparator、Filter、VoltageReference 改动及其 import 依赖均属于批准的发布范围**。当前审计不授权把这些混合文件作为整体加入。审查通过或在后续单独隔离出必要 hunk 后，才可用精确路径暂存：

```powershell
git add -- ActiveLearning/main.py ActiveLearning/active_learning.py ActiveLearning/experiment_logging.py
git add -- tests/test_active_learning.py tests/test_main.py tests/test_experiment_logging.py tests/test_inverse_designer.py
```

如果干净 SHA 的导入/回归要求当前其余 modified 的 `InitializeD0`、onboarding、D0、inverse design、predictor/evaluator 或任何 untracked circuit 文件，则先明确每一文件及资产的审查结果，再给出新的精确清单；**不要自动追加整目录**。暂存后先只读复核 `git diff --cached --name-status`、`git diff --cached --check` 和 `git diff --cached`，确认无 approval/claim、`.raw`、私钥、数据或意外实验结果，才允许后续人工 commit/push。本轮没有执行这些命令。

目标 GitHub 仓库尚未确定，因此不存在可信的现成 `git push` 命令或 Ubuntu `origin` 分支。只有当操作者确认 Ubuntu 隔离 clone 的 `origin` 就是实际发布仓库、同名分支已由人工推送、且该 clone 干净时，才可在**隔离 clone** 使用以下命令；不可在旧部署执行 `switch`：

```bash
repo=/home/ubuntu/AG_simulation_v2
git -C "$repo" remote -v
git -C "$repo" status --porcelain=v1 --untracked-files=all
git -C "$repo" fetch origin refs/heads/batch-runner-v2:refs/remotes/origin/batch-runner-v2
git -C "$repo" switch --create batch-runner-v2 --track origin/batch-runner-v2
git -C "$repo" rev-parse HEAD
git -C "$repo" ls-tree -r --name-only HEAD ActiveLearning/batch_runner_v2
git -C "$repo" show HEAD:ActiveLearning/main.py >/dev/null
```

`switch --create` 仅适用于隔离 clone 尚无本地同名分支时；若已有分支，先停下核对其 SHA 和未提交文件，不覆盖它。把 `rev-parse HEAD` 的完整 SHA 与人工发布记录逐字比较，再做隔离 venv/import、假子进程与 mock 测试。Ubuntu 现有 `~/AG_simulation_deploy` **不因本方案而变更**。

## 6. 解锁条件

1. 明确并验证 `AI-for-AnalogCkt` 与 `AG_simulation_deploy` 中哪个是唯一发布 remote，或明确受控跨仓库交付方法。
2. 对 mixed AL 改动做逐 hunk 发布审查，形成闭合的精确源码/测试/文档清单；排除一次性批准、claim、历史运行和数据。收紧或至少严格执行 path-only 暂存，并审查 staged diff。
3. 在一个干净固定 SHA 上确认 V2 与 AL 核心同版本导入与离线回归，记录 A2 0.2 s 时序波动和 Ubuntu Linux 实机结果。
4. 若 B1 要测试真实电路配置，只为批准的 job 另列哈希化运行资产清单并隔离传输；不将资产/审批默认为 Git 交付。

在满足这些条件前，不应发布为“Ubuntu V2-B1 可验证版本”，更不能作为生产 Batch 授权。
