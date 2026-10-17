# Batch Runner V2-B0 — 隔离 Ubuntu 部署前兼容性与回归收口

日期：2026-10-16。审查对象为 `D:\analogCkt\AG_simulation` 当前**未提交**工作树，HEAD `bcd5b48ce34b672a933c66b6b3b8d5061acc4b2b` **不包含本轮改动**。未连接 Ubuntu、未部署、未安装依赖、未执行真实 AL/D0/Profiling/SPICE/LLM、未改写历史实验或审批记录、未 commit/push。本报告的 GO 仅指可进入下一轮**隔离环境的离线部署审查**，不构成生产运行授权。

## 1. 文件与 AG626 回归

本轮仅修改 `ActiveLearning/tests/test_ag626_al_m7a.py`、`ActiveLearning/batch_runner_v2/al_process.py`、`ActiveLearning/batch_runner_v2/llm_bridge.py`、`tests/test_batch_runner_v2_a2.py`，新增本报告。V2-A/A2 的其他新文件与 `main.py` 已有修改均保留；没有修改 `ag626_al.py`、S0/D0、claim/approval、完成的 AL 结果或优化算法。当前仓库还有大量原有未提交内容，因此发布前必须单独审查并选择性提交，不能把当前 HEAD 当作已审 V2 发布版本。

AG626 原失败 `test_real_approval_preflight_is_read_only_and_transport_is_bounded` 直接断言仓库中的一次性 `al_execution_claim.json` 不存在；实际该文件已存在，生产预检正确地报告授权已消耗。这是测试的全局状态假设错误，不是生产门禁回归。测试现在通过 `tmp_path`/`monkeypatch` 使用临时 claim，分别验证未占用的只读预检、占用后 `live_al_authorized=False`、第二次 claim 拒绝，并核对真实 claim 字节哈希不变。真实 claim SHA-256 前后均为 `43d164a562d4d890a69c0463a2f1d8778a877c9185498761cd8c145f2d107c2d`；审批 YAML 前后均为 `9908b65b8f65c520b28d04ce06edd079556c12f7afeb404f2d53e6d2d3436335`。生产 AG626 0490 仍被通用 AL 入口和 V2 schema 拒绝；其 OpenAI/DeepSeek 一次性审批不可转换为 Gemini 批批准，也不可复用。

## 2. Linux 子进程隔离

A2 原实现仅能证明直接 Python 子进程退出，不能证明 ngspice 等后代退出；若调度器继续下一 job，会有实验并发风险。B0 的离线假子进程在 POSIX 上用 `Popen(start_new_session=True)` 创建独立进程组，超时先发 group `SIGTERM`，至多等待 2 秒；仍存活则发 `SIGKILL`，再至多等待 2 秒，并 `wait()` 回收直接子进程、用 group 探测确认后代退出。直接子进程正常退出但进程组仍活跃时也清理并把 attempt 标为不确定，不会误报完成。`Popen`/`wait`/`poll` 的 OS 错误以及无法确认退出的情形触发持久 `orphan_unverified`、停止后续 job，重启仍拒绝推进。已确认停止的超时 attempt 保留为 `INTERRUPTED_UNCERTAIN`，但独立后续电路可继续；同一 attempt 绝不自动重放。

这些 POSIX 分支只在 Windows 上以模拟 `killpg`/`wait` 验证了 TERM→KILL、超时、有异常时停止调度；**没有在 Linux 实机验证**。未来真实 AL/SPICE 还应考虑逃离进程组的后代、僵尸进程、PID/PGID 重用和 cgroup/pidfd 级证据；若不能排除孤儿必须保守阻断。Windows 直接子进程处理仅供假 child 测试，不能作为 Windows 真实后代隔离保证。`child_spawn_intent.json` 在 spawn 前持久化，`child_reaped.json` 仅在确认后写；调度器中断后无 reaped marker 不会启动下一 job。没有声称精确 AL iteration checkpoint。

## 3. Gemini 调用边界与会计

实际 AL 路径是 `main.run_configured_active_learning`→`active_learning.run_active_learning`→`_make_logged_llm_client`→逆向设计/预测。未注入时 `llm_client.call_inverse_design_llm` 自己重试（默认 30 秒/3 次），成功且有 usage 才写 AL log。V2 必须注入 `GeminiALClient`，直接调用 `GeminiRecovery.call`，不得再调用旧客户端重试函数。B0 新增 `build_gemini_chat_transport`：复用现有 Gemini 环境解析，以 OpenAI-compatible chat completions 构造客户端，SDK `max_retries=0`，每次物理发送显式传入恢复器给出的 timeout；没有新增批次真实 `run/execute` CLI。已用本地 OpenAI SDK 3.29.0 的 `Completions.create` 签名核对 `timeout` 参数，并用伪 SDK 验证调用，无网络请求。Gemini 兼容端实际错误体/usage 字段仍需单独真实验证。

恢复器为每个逻辑请求生成 ID，发送前持久 `attempt_started`，429 Retry-After/Google retryDelay、503/超时有界重试；401/403/确定每日 quota 终止，quota/物理 cap 按 provider/model/credential scope 阻断同 batch 后续 job。相同 batch root 顺序 job 共用 cooldown 和计数；跨 batch **不共享**，禁止在未协调情况下并行运行同凭据 batch。失败或缺失 usage 的物理尝试保留 `usage_unknown`，报告 `total_tokens=null`；只保证物理 request cap，**不保证 Token/金额硬上限**。账本只写 scope 标签、请求散列、状态/次数/时间/usage，不写完整 prompt、响应或 key；测试用 canary 验证未泄露。操作员必须使用非秘密的 `credential_scope` 标签。持久 cooldown 使用单调时钟，跨机器重启/重开机的时间语义尚未解决；不能据此批准生产自动恢复。实际 transport 虽可构造，仍未接入一个经授权的生产 child；端到端真实调用未验证。

## 4. Ubuntu 依赖/路径兼容矩阵

| 项目 | Windows 本地证据 | Ubuntu B1 必检项/结论 |
|---|---|---|
| Python | bundled Python 3.12.14；现成 `_codex_test_deps` 提供测试依赖 | `/home/ubuntu/AG_simulation_deploy/.venv/bin/python` 的实际版本、ABI、可执行权限；不能假定 3.12 或与固定 SHA 匹配 |
| NumPy/SciPy/PyYAML/PySpice/openai/pytest | 本地可导入，前五者版本 2.5.4/1.18.1/6.0.3/1.5/3.29.0 | 在 Ubuntu 同一 `.venv` 逐个 import；`pytest` 仅用于回归且未列入 `requirements-server.txt`，缺失时不得跳过而声称全测通过；文件仅列包名、无锁定版本，不能重装后声称复现 |
| ngspice | Windows PATH 中未找到可执行文件；本轮未运行模拟 | `command -v ngspice` 与 `ctypes.util.find_library('ngspice')`、`ctypes.CDLL` 都需确认；`run/single_stage_amplifier/run_ideal_dataset_simulations.py` 的 Linux 分支可用 `NGSPICE_LIBRARY_PATH` 覆盖，命令存在不等于共享库可装载 |
| Gemini transport | 本地 SDK 签名与伪响应已验证 | `GEMINI_API_KEY`（仅进程环境）、`GEMINI_MODEL`、可选 `GEMINI_BASE_URL`，实际兼容端格式/429 headers/usage 尚未验证；不得输出 key |
| Python 导入路径 | 以仓库根为 cwd、`PYTHONPATH` 含仓库根可导入 | detached release 必须从其根运行或显式设 `PYTHONPATH=<release>`；禁止误导入旧 checkout 的 `ActiveLearning` |
| dataset/配置/D0 | `main._resolve_paths` 与 AutoConfig 多处从仓库根 `dataset/<circuit_id>` 加载；冻结配置/D0 路径按电路各异 | 先按**具体批准的 job 清单**枚举 topology、参数/模型、配置、D0、Descriptor 的原始与 `Path.resolve()` 路径及 SHA；不能假定所有 dataset 都由 Git 跟踪或 Batch 006 与 005 相同 |
| 外部符号链接 | V2 `schema._asset` 要求 `resolve()` 后留在 manifest 父目录 | 指向旧 checkout 的 dataset/D0 symlink 会被拒绝；B1 应在全新 release 内受控提供已哈希校验的只读资产副本，或另行审查明确的只读映射规则，不能削弱 `resolve()` 限制 |
| 输出/权限 | 假进程只在显式临时 root 写入 | release 源/输入只读，批输出另设可写隔离目录；确认 owner、mode、可用空间；不覆盖旧 `ActiveLearning/runs`、AG626 runs 或旧 batch_runs |
| 代码身份 | 当前 HEAD 不含未提交 V2 代码 | 人工审查后选择性 commit/push，B1 仅用固定 SHA `git worktree add --detach`；Ubuntu checkout staged/unstaged/untracked/ignored 文件都不 reset/clean |

最小代码交付不是复制旧 Ubuntu worker，而是固定 SHA 的当前 AL 核心、`ActiveLearning/batch_runner_v2`、`ActiveLearning/main.py`、`ActiveLearning/onboarding`、`InitializeD0`、circuit types、`inverse_design`、预测/采集/validity、`run`/`simulation_metrics`、Stage A/B 及其运行依赖。数据资产仅由明确批准的 circuit 清单决定；没有生产 manifest 就不能安全给出“最小 dataset 文件列表”。历史 Descriptor 内若有绝对路径，需要保留原身份校验并逐项验证可达性，不得为通过校验改写历史文件。

## 5. 授权边界与 B1 建议

V2 manifest 目前只绑定 job/config/D0/source 哈希、provider/model、迭代数、seed 和物理尝试 cap；**没有**固定 Git SHA、正式 batch 批准记录/签名、目标全量身份、Token/金额硬预算，也没有真实执行入口。因此现有机制不能被解释为生产授权。`main.py` 的 `require_existing_d0=True` 与 expected hashes 是未来执行时必传的防再生检查，静态 V2 `plan` 不能替代原 AL 入口/Book625 S0 守卫。AG626 使用一次性 OpenAI/DeepSeek 批准，通用入口与 V2 都拒绝 0490；Book625 当前配置要求本地 OpenAI provider，与 V2 仅接受 Gemini 的 manifest 不兼容。这些冲突需按电路另行审批/迁移，不能让 batch 批准覆盖或刷新旧 claim。历史 Batch 005 的预算暂停/partial 会计保持原状，V2 不能用新 batch 绕过。

**B1 建议：GO，仅限固定 SHA 的隔离 Ubuntu 只读预检与假子进程/模拟回归；生产 AL/LLM/SPICE 仍 NO-GO。** 前提是人工先将本轮精确差异选择性提交并完成代码/安全审查，发布经确认的 SHA；先保护旧 checkout 所有本地修改与 ignored 数据，再为具体 job 提供经哈希校验的隔离资产。不能把当前 `bcd5…` HEAD 或 Windows mock 结果直接当 Ubuntu 发布证据。

### 未来操作员命令（本轮未执行）

先在 Ubuntu **只读**，把人工审核后发布的完整 SHA 填入 `reviewed_sha`；不在旧 checkout 运行可变脚本：

```bash
set -euo pipefail
repo=/home/ubuntu/AG_simulation_deploy
reviewed_sha='<REVIEWED_FULL_SHA>'
git -C "$repo" status --porcelain=v1 --untracked-files=all
git -C "$repo" rev-parse HEAD
git -C "$repo" cat-file -t "$reviewed_sha"
git -C "$repo" show "$reviewed_sha:ActiveLearning/batch_runner_v2/al_process.py" >/dev/null
py="$repo/.venv/bin/python"
test -x "$py"
"$py" -V
"$py" -c 'import ctypes,ctypes.util,importlib; [importlib.import_module(x) for x in ("yaml","numpy","scipy","PySpice","openai","pytest")]; p=ctypes.util.find_library("ngspice"); print("ngspice_library_found", bool(p)); p and ctypes.CDLL(p)'
command -v ngspice || true
```

只有上述身份/环境检查和人工资产清单通过后，B1 才可**另行批准**在全新路径执行以下隔离写入；命令不会 reset/clean 旧 checkout，也不会启动真实批次：

```bash
release="/home/ubuntu/AG_simulation_releases/$reviewed_sha"
test ! -e "$release"
git -C "$repo" worktree add --detach "$release" "$reviewed_sha"
test "$(git -C "$release" rev-parse HEAD)" = "$reviewed_sha"
cd "$release"
PYTHONPATH="$release" "$py" -m unittest -q tests.test_batch_runner_v2 tests.test_batch_runner_v2_recovery tests.test_batch_runner_v2_a2 tests.test_experiment_logging tests.test_active_learning tests.test_main tests.test_inverse_designer
PYTHONPATH="$release" "$py" -m pytest -q ActiveLearning/tests/test_book625_al_m4.py ActiveLearning/tests/test_book625_local_al5_config.py ActiveLearning/tests/test_ag626_al_m7a.py
```

这组命令尚不复制 dataset/D0，也不执行 `plan`，因为尚无审核过的 job manifest/资产清单；若任一资产在 release 不可达或哈希不符，应停止，不使用旧目录符号链接绕过 V2 路径限制。真实 Gemini/SPICE 验证、正式预算与批次批准、进程组实机证据、Ubuntu 版本/权限和跨批额度协调均为之后独立门槛。
