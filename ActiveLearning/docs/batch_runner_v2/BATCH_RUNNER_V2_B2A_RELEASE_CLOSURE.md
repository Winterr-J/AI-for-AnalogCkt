# Batch Runner V2-B2A-R — Real AL Release Closure

## 结论

发布候选状态：`V2_B2A_RELEASE_CANDIDATE_READY`。

这里的 READY 仅表示：以冻结基线
`e0276463b179b161dd63819a9107bda9bcf782e2` 为起点，可以用明确的最小补丁和逐文件清单构造
一个供操作员审查、提交及 Ubuntu 离线验证的候选。它不代表已有固定的新 Git SHA，不代表
CurrentMirror 已获真实运行批准，也不授权 Gemini、SPICE、D0、Profiling 或 AL。

当前脏工作树不能直接暂存。隔离验证证明不需要整体发布其中混合的 Book625、AG626、Comparator、
Filter、VoltageReference 或其他研究改动；`ActiveLearning/main.py` 必须从基线应用本报告附带的最小
patch，不能从当前工作树整文件复制。

配套可复现材料：

- `BATCH_RUNNER_V2_B2A_SOURCE_CLOSURE.json`：115 个本地源文件、8 个运行资产和 4 个测试文件的
  精确相对路径与 SHA-256；文件自身 SHA-256 为
  `ad74cbbbc3e4f87e0d9b37980de11b0538b52baf47e09365ad34fa5270a5974d`。
- `BATCH_RUNNER_V2_B2A_MINIMAL_MAIN.patch`：只增加 B2A 所需冻结输入、专用输出根和 callback 接口；
  SHA-256 为 `99a180760ee5fffc3b3ac6911dd696ae7b811764f4bba57fcf1eb16729084c33`。

## 实际 production 调用链

实际代码路径（不是设计推断）为：

```text
runner.run_authorized_batch
  -> authorization.validate_authorization / claim_authorization
  -> runner._run_batch
  -> al_process.run_real_al_child
  -> python -m ActiveLearning.batch_runner_v2.real_al_child
  -> real_al_child.execute_invocation
  -> validate manifest / authorization / claim / scheduler RUNNING attempt
  -> real_al_child._production_run
  -> GeminiRecovery + build_gemini_chat_transport(max_retries=0) + GeminiALClient
  -> main.run_configured_active_learning
  -> AutoConfig + frozen config/D0 loading
  -> active_learning.run_active_learning
  -> inverse design -> data expansion -> performance prediction -> acquisition
  -> real_evaluation -> CurrentMirror evaluator -> PySpice/ngspice
  -> experiment_logging summary
  -> worker_result identity/hash validation
  -> scheduler result commit
```

父进程只能启动固定 Python 模块，不能传入任意 command/module。子进程再次检查 attempt 必须位于
`<output>/<batch>/jobs/<job>/attempts/<attempt>`，并且是 scheduler 状态中最后一个 `RUNNING`
attempt。生产 scope 同时要求外部授权 digest、显式 `enable_production=True`、环境中的同一授权
digest，以及所有 closure 文件与授权 Git revision 字节一致。

## 精确依赖闭包

完整逐文件闭包在 JSON manifest 中，不能由本文摘要替代。构造方法是：在 detached 基线中仅叠加
B2A 文件与最小 main patch；执行 CurrentMirror 的无网络、无 SPICE runtime preflight；记录实际加载
的仓库内模块，再补入固定子进程/CLI、动态 topology、prompt、配置和测试资产。随后对 127 个条目
逐字节复核，结果为 `missing=[]`、`mismatched=[]`。

分类如下：

| 类别 | 数量与内容 | 发布处理 |
|---|---|---|
| A. 基线已有源 | closure 中 109 个未变化源文件，包括 AL 算法、AutoConfig、Stage A/B、CurrentMirror evaluator、prompt loader、PySpice adapter 和原 V2-B1 | 不重新复制或暂存；由基线 SHA 提供 |
| B. 新 B2A 源 | `authorization.py`、`real_al_child.py` | 精确复制并逐文件暂存 |
| C. 修改现有源 | `main.py`、`batch_runner_v2/al_process.py`、`runner.py`、`schema.py` | `main.py` 只允许应用最小 patch；另外三项复制已验证 B2A 版本 |
| D. 测试 | 基线已有 3 个 V2 测试；新增 `tests/test_batch_runner_v2_b2a.py` | 仅新增测试需暂存 |
| E. Git 中的 runtime assets | CurrentMirror config、D0、ideal model、performance template、四个 prompt；topology 计入 source closure | 全部已存在基线，不重新暂存；生产 manifest 应把 JSON 的 `source_files` 与 `runtime_assets` 合并进 closure 校验 |
| F. 非 Git runtime | Python/PySpice/ngspice、Gemini credential、授权文件、claim、batch output | 不提交；在 Ubuntu 独立验证和受控目录中提供 |

当前 schema 强制一个最低 closure，但无法自行知道某个 circuit 的所有条件式动态依赖。因此
CurrentMirror 的生产 manifest 必须使用本次 115+8 的完整集合，不能只使用 schema 中的最低集合。
配置、D0 和 topology 还应同时作为 job asset 绑定。

本次候选环境记录为 Python 3.12.14、NumPy 2.5.4、SciPy 1.18.1、PyYAML 6.0.3、PySpice 1.5、
openai 3.29.0；这只是 Windows 隔离测试证据，不是 Ubuntu 版本要求或安装授权。

## Mixed AL core 的处理

对 `main.py` 的逐 hunk 分析表明，B2A 只需要：

1. `hashlib`；
2. `runs_root` 与 `run_context_callback`；
3. `require_existing_d0`；
4. `expected_config_sha256` 与 `expected_d0_sha256`；
5. 将 create/write 的输出根统一为专用 attempt root。

隔离候选保留基线版本的 `active_learning.py`、`experiment_logging.py`、D0、AutoConfig、MMR、
inverse design、performance prediction、evaluator 和 circuit guards。59 项 V2 测试与定向 preflight
均不需要当前脏工作树中这些文件的修改。因此以下当前修改明确排除：

- `.gitignore`；
- `InitializeD0/d0_builder.py`、evaluator/validation `__init__.py`；
- `active_learning.py`、`auto_target.py`、`common/mmr.py`、`d0_initialization.py`；
- `data_expansion/data_expander.py`、`experiment_logging.py`；
- `inverse_design/inverse_designer.py`、`inverse_design/prompt_builder.py`；
- `onboarding/auto_config.py`、`models.py`、`runtime_provider.py`；
- `performance_prediction/performance_predictor.py`、`profiling.py`、`real_evaluation.py`；
- `tests/test_active_learning.py` 与 MotorDriver `.asc` 修改；
- 所有未跟踪 Comparator、Filter、LDO、VoltageReference、Book625、AG626、实验目录、raw 输出、
  profiling/D0 结果及其测试。

不会发布 approval/claim、`.env`、API key、私钥、历史 run/batch 状态、服务器备份或下载的
deployment 文件。

## 隔离验证结果

隔离目录使用真正的 detached Git worktree，HEAD 为 `e027646…`；它没有从原脏工作树加入
PYTHONPATH，只叠加 6 个 B2A 文件和最小 `main.py` patch。

| 验证 | 结果 |
|---|---|
| V2 全套 `test_batch_runner_v2*.py` | 59/59 PASS，29.470 s |
| Mock-only AL、inverse design 与 CurrentMirror adapter/error paths | 34/34 PASS，0.055 s |
| 实际 main/AutoConfig/D0 的 no-call preflight | PASS；20 条 D0、max_iterations=5、专用 output callback、config/D0 hash、topology path 均匹配；外部调用 0 |
| import provenance | PASS；加载的 `ActiveLearning.*` 全部来自隔离候选 |
| closure byte verification | 127/127 匹配 |
| `git diff --check` | PASS；只有 Windows line-ending 提示 |

最初使用 `git archive` 的尝试因没有 `.git` 元数据，被 `current_source_revision()` 正确拒绝；因此
不能用 archive 结果冒充 revision-bound 验证。最终结果来自 detached worktree。

一次额外定向命令误包含了会启动真实 ngspice 的
`test_current_mirror_evaluator::test_circuit_0469_nominal_regression`。该测试返回
`simulation_valid=False`，所以当次集合为 34 PASS、1 FAIL。没有调用 LLM、没有写研究结果，也没有
继续或重跑该真实测试。它不是允许范围内的 Fake SPICE 证据，并明确留下 Ubuntu PySpice/ngspice
兼容性 blocker。

没有“clean fixed candidate SHA”结论：候选尚未提交，closure JSON 的 `candidate_revision` 保持
`null`。把未来 SHA 写进同一个产生该 SHA 的提交会形成自引用；实际 release SHA 应由提交后的外部
发布记录和生产 batch manifest 绑定。

## CurrentMirror/circuit_0469 评估

固定 Git 内容的相关哈希（不是当前 Windows CRLF 工作文件哈希）：

| 资产 | SHA-256 |
|---|---|
| topology `dataset/circuit_0469/topology/circuit_0469.py` | `3e0095a5fd17e5622c9519bf6b5f03e1b5b1a5aa7b46cc4b1e4bd9208005d6f5` |
| config | `491a0fec62e5eca9d715938e39876bcdacfefa59385b77b9e54f443e636f820d` |
| D0 JSONL | `945c03209c038e9a1b97929093a660a9d5bffc51e3637dfcb1c97545f19b86fe` |
| ideal model/parameters | `7ec6dc7bc7d7023f6f93610ae4298ff323338feacae416643c9e12ce92e4b435` |
| performance template | `c3927d80af76489ca6689e80e26149e22378b9c8d206146ea9555438d4cd42da` |
| canonical target spec | `1ef24de5f1c8272fb93579ceb2f6340abde7f41fc61902ce613f81035d3a7a15` |

Windows 原工作文件的 config 哈希 `612c…` 是 CRLF 字节结果；clean Git/Ubuntu 内容是 `491a…`。
manifest 必须从最终 release checkout 的实际字节生成。

可用性结论：

- topology、config、performance target、20 条 D0 和内联 `NPN_IDEAL` 模型均存在且在基线中 tracked；
- config 最大 5 轮；当前 target 重新计算为 0/20 达标，不会依赖旧 `meets_spec` 字段提前成功；
- Gemini provider/model 不在该 config 中静默决定，而由 B2A manifest 和 authorization 精确绑定；
- 仓库中未发现 circuit_0469 的一次性 approval/claim，因此没有可复用或已消费 claim；真正运行仍必须
  创建新的 batch-scoped 正式授权；
- D0 目录只有 JSONL，没有 CSV/initialization manifest，config 也未声明 `d0_mode: independent`。
  因此可证明“存在且 hash 稳定”，但不能仅凭仓库证明“独立 D0 的来源/初始化身份”；
- Windows nominal real evaluator 本轮未通过，Ubuntu 的 ngspice 可执行文件、共享库和数值回归尚未验证。

所以 circuit_0469 可作为下一轮 Ubuntu **离线兼容性候选**，尚不适合作为获批真实 smoke。现有替代中，
Book625 0489 有冻结独立 D0 但明确要求 local OpenAI-compatible provider；AG626 0490 受一次性 guard
约束且 generic AL 被禁用；没有找到同时满足 Gemini B2A、独立 D0 provenance、未消费授权和已验证
Ubuntu evaluator 的替代 circuit。不能用它们绕过当前 blocker。

## 资源与授权安全

- `GeminiRecovery` 在发送前记录每个 physical attempt，重试也占用 batch-root 共享 cap；达到 cap
  写 provider block 并停止。
- 429 使用 Retry-After/retryDelay；5xx/timeout/network 受每 logical call 的 attempts 和 elapsed
  deadline 限制；SDK `max_retries=0`，没有嵌套 retry owner。
- daily/provider quota 阻断 scope；认证错误 terminal；unknown usage 保持 unknown，不转成零。
- cleanup 已确认的 terminal job 可继续无关 job；uncertain attempt 不自动 replay；未确认 orphan
  阻断 batch。
- authorization 绑定 batch、Git revision、closure、job assets、provider/model、iteration、retry、
  physical cap 和 output root；claim 不能转移到新 batch/output。

限制：physical cap 是一个 batch output root 内的强制上限，不是不同 batch/不同目录之间的全局账户
上限；provider cooldown/block 也不是跨 batch 全局协调。`max_elapsed` 是每个 logical request 的期限，
不是整个 batch 的墙钟上限。provider 不返回 usage 时无法强制 token 或金额上限，因此不能宣称 strict
spending cap。首次运行必须避免同 credential scope 并发 batch，并由人工审批物理请求上限。

授权 JSON + 外部 digest 是 fail-closed 机制，不是数字签名或审批机构本身。代码审查、测试通过或本
报告都不能签发生产批准。

## 精确 Git 构造与暂存提案（仅供批准后执行）

不要在当前脏工作树暂存。以下 PowerShell 命令从 approved base 创建独立 review branch；本轮没有执行
这些 add/commit/push 命令。

```powershell
$source = 'D:\analogCkt\AG_simulation'
$release = 'D:\analogCkt\AG_simulation_v2_b2a_release'
$base = 'e0276463b179b161dd63819a9107bda9bcf782e2'

git -C $source worktree add -b codex/batch-runner-v2-b2a-release $release $base
git -C $release rev-parse HEAD
git -C $release status --porcelain

$mainPatch = Join-Path $source 'ActiveLearning\docs\batch_runner_v2\BATCH_RUNNER_V2_B2A_MINIMAL_MAIN.patch'
git -C $release apply --check $mainPatch
git -C $release apply $mainPatch

$copy = @(
  'ActiveLearning\batch_runner_v2\al_process.py',
  'ActiveLearning\batch_runner_v2\authorization.py',
  'ActiveLearning\batch_runner_v2\real_al_child.py',
  'ActiveLearning\batch_runner_v2\runner.py',
  'ActiveLearning\batch_runner_v2\schema.py',
  'tests\test_batch_runner_v2_b2a.py',
  'ActiveLearning\docs\batch_runner_v2\BATCH_RUNNER_V2_B2A_REAL_AL_INTEGRATION.md',
  'ActiveLearning\docs\batch_runner_v2\BATCH_RUNNER_V2_B2A_MINIMAL_MAIN.patch',
  'ActiveLearning\docs\batch_runner_v2\BATCH_RUNNER_V2_B2A_SOURCE_CLOSURE.json',
  'ActiveLearning\docs\batch_runner_v2\BATCH_RUNNER_V2_B2A_RELEASE_CLOSURE.md'
)
foreach ($path in $copy) {
  $destination = Join-Path $release $path
  New-Item -ItemType Directory -Force -Path (Split-Path $destination) | Out-Null
  Copy-Item -LiteralPath (Join-Path $source $path) -Destination $destination
}

git -C $release status --short
git -C $release diff --check
```

人工复核内容与 closure 后，才可逐路径暂存：

```powershell
git -C $release add -- ActiveLearning/main.py
git -C $release add -- ActiveLearning/batch_runner_v2/al_process.py
git -C $release add -- ActiveLearning/batch_runner_v2/authorization.py
git -C $release add -- ActiveLearning/batch_runner_v2/real_al_child.py
git -C $release add -- ActiveLearning/batch_runner_v2/runner.py
git -C $release add -- ActiveLearning/batch_runner_v2/schema.py
git -C $release add -- tests/test_batch_runner_v2_b2a.py
git -C $release add -- ActiveLearning/docs/batch_runner_v2/BATCH_RUNNER_V2_B2A_REAL_AL_INTEGRATION.md
git -C $release add -- ActiveLearning/docs/batch_runner_v2/BATCH_RUNNER_V2_B2A_MINIMAL_MAIN.patch
git -C $release add -- ActiveLearning/docs/batch_runner_v2/BATCH_RUNNER_V2_B2A_SOURCE_CLOSURE.json
git -C $release add -- ActiveLearning/docs/batch_runner_v2/BATCH_RUNNER_V2_B2A_RELEASE_CLOSURE.md

git -C $release diff --cached --check
git -C $release diff --cached --name-status
git -C $release diff --cached --stat
```

期望 staged 集合只能是上述 11 个路径。特别不得执行 `git add .`、`git add -A` 或暂存整个
`ActiveLearning/`。commit/push 必须等待操作员批准；目标 remote 应保持
`https://github.com/Winterr-J/AI-for-AnalogCkt.git`，通过 review branch 审核后再进入
`batch-runner-v2`。

## 固定 SHA 与 Ubuntu 离线验证程序

人工提交后，在 Windows release worktree 先记录：

```powershell
$sha = git -C $release rev-parse HEAD
git -C $release status --porcelain
git -C $release diff --name-status $base $sha
git -C $release ls-tree -r --name-only $sha ActiveLearning/batch_runner_v2
```

`status --porcelain` 必须为空，路径列表必须与批准集合一致。然后从 clean checkout 重跑 59 项测试和
closure checker。不要把 closure JSON 的 `candidate_revision=null` 改成提交自身 SHA；将 `$sha` 写入
外部 release record 和之后的 B2A batch manifest。

Ubuntu 操作员在独立目录、明确 SHA 上执行；不要修改旧部署：

```bash
set -eu
repo=/home/ubuntu/AG_simulation_v2_b2a_rc
sha='<operator-approved-full-sha>'
git clone --no-checkout https://github.com/Winterr-J/AI-for-AnalogCkt.git "$repo"
git -C "$repo" fetch origin batch-runner-v2
git -C "$repo" checkout --detach "$sha"
test "$(git -C "$repo" rev-parse HEAD)" = "$sha"
test -z "$(git -C "$repo" status --porcelain)"

V2_PYTHON='<path-to-the-previously-verified-independent-v2-python>'
test -x "$V2_PYTHON"
"$V2_PYTHON" -c 'import sys,numpy,scipy,yaml,PySpice,openai; print(sys.executable); print(sys.version); print(numpy.__version__,scipy.__version__,yaml.__version__,PySpice.__version__,openai.__version__)'
command -v ngspice
ngspice --version
ldconfig -p | grep -i ngspice

cd "$repo"
env -u GEMINI_API_KEY -u GOOGLE_API_KEY -u OPENAI_API_KEY PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$repo" \
  "$V2_PYTHON" -m unittest discover -s tests -p 'test_batch_runner_v2*.py' -v
```

不安装或升级包。若解释器、PySpice、ngspice executable/shared library 或 closure hash 不一致，停止并
报告。Ubuntu 可以运行 Fake Gemini/Fake SPICE 的 59 项测试和只读 closure/hash preflight；不得调用
`run_authorized_batch(... enable_production=True)`，不得创建 production authorization/claim，不得运行
真实 nominal evaluator，直到另一次明确批准。

Ubuntu 下一轮必须补齐：Linux process-group/orphan 证据、127 条 closure 复核、CurrentMirror
config/D0/topology 的 checkout-byte hash、PySpice/ngspice import/ABI，以及只使用 fake transport 的
三任务 batch。真实 circuit_0469 smoke 仍需 D0 provenance 审批、通过的 Ubuntu evaluator 兼容性证据、
新 batch authorization 和单独的生产批准。
