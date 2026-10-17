# Batch Runner V2 — 组合测试稳定性调查

日期：2026-10-17。仓库：`D:\analogCkt\AG_simulation`。结论：**本地 V2 组合离线测试稳定性问题已按实际根因修复**；Git 发布与 Ubuntu B1 仍为 **NO-GO**，理由见第 6 节。本轮没有暂存、提交、推送、部署，也没有运行真实 AL、SPICE 或 LLM。

## 1. 复现命令与逐次结果

先完整阅读 `V2_GIT_RELEASE_AUDIT.md` 与 `BATCH_RUNNER_V2_B0_READINESS.md`。使用前次审计记录的**同一 Windows 组合命令**，逐次独立启动 Python：

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
& 'C:\Users\clair\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -q tests.test_batch_runner_v2 tests.test_batch_runner_v2_recovery tests.test_batch_runner_v2_a2
```

| 修改前运行 | 结果 |
|---|---|
| 审计原运行 | 46 项：1 failure、1 error（下节两案）。 |
| 本轮 #1 | 46 项 PASS，13.336 s。 |
| 本轮 #2 | 46 项中 1 failure：`test_crash_nonzero_partial_corrupt_remain_uncertain` 的 `corrupt` 子场景，13.687 s。 |
| 本轮 #3 | 46 项 PASS，12.474 s。 |
| 本轮 #4 | 46 项 PASS，13.759 s。 |
| 本轮 #5 | 46 项中 1 failure：同一测试的 `crash` 子场景，12.919 s。 |
| 本轮 #6 | 46 项 PASS，13.351 s。 |
| 本轮 #7 | 46 项 PASS，13.225 s。 |

修改前本轮复现为 **2/7 次失败**；原审计的 error 案在本轮七次中未再次出现，不能将它称作本轮新复现。两种失败都涉及本应正常完成的 fake child 被判为不确定。

## 2. 完整失败堆栈

前次发布审计运行记录的两项原始失败（临时目录名仅用于说明当次执行，不是共享路径）：

```text
======================================================================
ERROR: test_completed_child_artifact_corruption_blocks_on_restart (tests.test_batch_runner_v2_a2.A2Tests.test_completed_child_artifact_corruption_blocks_on_restart)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "D:\analogCkt\AG_simulation\tests\test_batch_runner_v2_a2.py", line 87, in test_completed_child_artifact_corruption_blocks_on_restart
    child_path = Path(json.loads(result_path.read_text(encoding="utf-8"))["result_path"])
                                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\clair\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\Lib\pathlib.py", line 1027, in read_text
    with self.open(mode='r', encoding=encoding, errors=errors) as f:
         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\clair\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\Lib\pathlib.py", line 1013, in open
    return io.open(self, mode, buffering, encoding, errors, newline)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
FileNotFoundError: [Errno 2] No such file or directory: 'C:\\Users\\clair\\AppData\\Local\\Temp\\tmpipub7oot\\outputs\\offline_a2\\jobs\\job_0\\attempts\\0cc5d4ec801f41a0bbe2887445d46917\\result.json'

======================================================================
FAIL: test_crash_nonzero_partial_corrupt_remain_uncertain (tests.test_batch_runner_v2_a2.A2Tests.test_crash_nonzero_partial_corrupt_remain_uncertain) (scenario='crash')
----------------------------------------------------------------------
Traceback (most recent call last):
  File "D:\analogCkt\AG_simulation\tests\test_batch_runner_v2_a2.py", line 99, in test_crash_nonzero_partial_corrupt_remain_uncertain
    self.assertEqual((report["interrupted_uncertain"], report["completed"]), (1, 1))
AssertionError: Tuples differ: (2, 0) != (1, 1)

First differing element 0:
2
1

- (2, 0)
+ (1, 1)

----------------------------------------------------------------------
Ran 46 tests in 11.920s

FAILED (failures=1, errors=1)
```

本轮 #2 的完整失败：

```text
======================================================================
FAIL: test_crash_nonzero_partial_corrupt_remain_uncertain (tests.test_batch_runner_v2_a2.A2Tests.test_crash_nonzero_partial_corrupt_remain_uncertain) (scenario='corrupt')
----------------------------------------------------------------------
Traceback (most recent call last):
  File "D:\analogCkt\AG_simulation\tests\test_batch_runner_v2_a2.py", line 99, in test_crash_nonzero_partial_corrupt_remain_uncertain
    self.assertEqual((report["interrupted_uncertain"], report["completed"]), (1, 1))
AssertionError: Tuples differ: (2, 0) != (1, 1)

First differing element 0:
2
1

- (2, 0)
+ (1, 1)

----------------------------------------------------------------------
Ran 46 tests in 13.687s

FAILED (failures=1)
```

本轮 #5 同一断言、同一 `(2, 0) != (1, 1)`，子场景为 `crash`，46 项执行 12.919 s。

## 3. 根因判定

`tests/test_batch_runner_v2_a2.py` 的 `worker()` 原先不分场景把 `timeout_seconds=0.2` 传给每个 `run_offline_fake_child()`。适配器在 `Popen` 后立刻 `process.wait(timeout=...)`；因此这个 0.2 秒不仅限制故意 `sleep` 的场景，也限制 `success`/`invalid`/`terminal` 的 Python 解释器启动、模块导入、JSON 处理和结果文件落盘。Windows 调度与前序测试的文件/进程活动足以偶发超过该预算。超时后适配器按安全设计停止并确认子进程，将尝试记为 `INTERRUPTED_UNCERTAIN`，于是成功 job 的 `result.json` 不存在，或报告从预期 `(1 uncertain, 1 completed)` 变为 `(2 uncertain, 0 completed)`。

证据：第一案中预期成功的 `job_0` 未留下 `result.json`，第二案中预期成功的后续 `job_1` 被记为不确定；相同代码多次组合运行有时全过、有时失败；单独 A2 曾 15 项全过。三组测试的 `setUp()` 均用各自 `tempfile.TemporaryDirectory()`，A2 自身每次又在其私有 root 下建 manifest/output，未发现共享的持久测试目录。fake child 只按唯一 attempt 写结果，`sleep` 是唯一故意长期运行的场景。当前证据支持**测试夹具的超时语义错误，加上启动/调度时间波动**；没有证据支持跨测试状态污染、Gemini 重试或生产 AL 缺陷。Windows 对 POSIX 进程组仍只是模拟测试，不能据此证明 Ubuntu 后代清理。

## 4. 最小修复与安全边界

只修改 `tests/test_batch_runner_v2_a2.py` 的 `worker()`：仅 `sleep` 场景显式使用 0.2 秒，以维持确认超时/终止保护的测试；所有应立即完成的场景不覆盖 `run_offline_fake_child()` 原有 **5 秒默认值**。这是按场景恢复既有超时契约，不是全局延长生产 timeout。未修改 `al_process.py`、`fake_al_child.py`、runner、AL 核心、Gemini recovery/bridge、授权、历史结果或任何生产执行入口。

保护范围仍由同一套测试覆盖：已确认停止的 timeout 留作不确定、后续独立 job 可执行；缺 `child_reaped` 的重启不自动重放；未确认 orphan 时停止后续调度；POSIX TERM→KILL 与无法确认进程组退出的模拟分支仍测试；Gemini fake transport 的 `max_retries=0` 和唯一 recovery retry owner、429 两次物理尝试仍测试。此次修复不改变这些实现。

## 5. 修改后回归

| 验证 | 结果 |
|---|---|
| 相同三组 Windows 组合命令 #1—#6（独立 Python 进程） | 每次 46 tests、PASS；耗时分别 13.078、14.171、14.147、13.058、13.058、13.424 s。两项原失败测试每次均执行。 |
| AL 核心：`tests.test_experiment_logging tests.test_active_learning tests.test_main tests.test_inverse_designer` | `PYTHONPATH` 包含本地已有 `_codex_test_deps` 与仓库根；51 tests，PASS，0.583 s。 |
| B0 的 V2 + AL 同进程离线回归（上述 7 个 unittest 模块） | 同一依赖路径，97 tests，PASS，14.994 s。没有运行 Book625/AG626 的额外 pytest 资产回归。 |

此结果只证明**当前 Windows 工作树**的离线稳定性改善。有限的 6 次通过不能证明绝无调度波动；Linux 实机进程组、干净固定 SHA 以及隔离 Ubuntu venv 仍须按 B0/B1 单独验收。测试使用临时合成资产和 fake transport/child，没有真实 LLM、SPICE、AL 或 D0。

测试中的确认退出、未确认 orphan 阻断等断言均通过。额外的 Windows 进程表只读查询被系统拒绝访问，因此本轮不能独立声称“系统中不存在任何假子进程”；这不影响测试内的 reaping 断言，但仍是进程清理证据的边界。

## 6. 发布影响、最小文件集与 Go/No-Go

本轮新增到候选清单的只有改过的 `tests/test_batch_runner_v2_a2.py`（它本已列在 `V2_GIT_RELEASE_AUDIT.md` 的三项 V2 测试中）和本报告；**不新增任何生产代码或运行依赖**，B0 的隔离部署模型不变。沿用发布审计的最小 mock 层：`ActiveLearning/batch_runner_v2/` 中明确列出的 9 个 `.py`、`tests/test_batch_runner_v2.py`、`tests/test_batch_runner_v2_recovery.py`、`tests/test_batch_runner_v2_a2.py`、`BATCH_RUNNER_V2_B0_READINESS.md`、`V2_GIT_RELEASE_AUDIT.md` 与本报告。若发布包含 AL 集成回归，另须按该审计逐 hunk 审查 `ActiveLearning/main.py`、`active_learning.py`、`experiment_logging.py`，以及 `tests/test_active_learning.py`、`test_main.py`、`test_experiment_logging.py`、`test_inverse_designer.py`；仍需核对其余动态导入依赖。真实 circuit 资产、approval/claim、历史数据、`.raw` 和密钥不属于该最小 Git 文件集。

**本地稳定性：GO。Git 发布 / Ubuntu B1：NO-GO。** 本地 `origin` 为 `Winterr-J/AI-for-AnalogCkt`，Ubuntu 旧部署来自独立的 `Winterr-J/AG_simulation_deploy`；当前 HEAD 不含 V2 包，工作树混有未提交改动，尚无可供 Ubuntu 验证的同一固定提交。不得合并无关历史、改 remote、推送或在旧部署目录切换分支。既有 `V2_GIT_RELEASE_AUDIT.md` 的发布阻断仍有效；其组合测试失败记录是历史证据，本报告仅收口该特定时序问题，不修改原审计。

最终状态：`V2_COMBINED_TEST_STABLE`（仅指本地组合离线测试）。
