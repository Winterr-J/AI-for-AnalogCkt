# Batch Runner V2-B2A Real AL Worker Integration

## 结论与边界

状态：`BATCH_RUNNER_V2_B2A_OFFLINE_READY`

V2-B2A 已建立一条可离线验证的真实 AL worker 边界，并通过 Fake Gemini / Fake SPICE
端到端测试。生产执行仍处于禁用状态。本里程碑没有执行 Gemini、SPICE、D0 初始化、
Profiling 或真实 AL，也没有部署、提交或推送。

此结论不是生产批准。当前工作树的 Git HEAD 仍为冻结的 V2-B1 提交
`e0276463b179b161dd63819a9107bda9bcf782e2`，而真实 AL core 和 B2A 文件包含未提交内容；
`verify_published_source_closure()` 会因此阻止生产执行。必须先得到一个独立审查并发布的
新固定提交，再生成 manifest 和外部授权。

## 精确改动

- `ActiveLearning/batch_runner_v2/schema.py`
  - 保留 V2-A schema，新增严格的 `batch_runner_v2b2a_candidate_v1`。
  - 绑定 source revision、source closure、配置、D0、拓扑、target spec、迭代与资源限制。
  - 直接资产不允许符号链接；路径必须位于 manifest 根目录且 SHA-256 匹配。
- `ActiveLearning/batch_runner_v2/authorization.py`
  - 校验外部固定的授权文件 SHA-256，绑定完整 batch identity 与输出根目录。
  - 校验当前 HEAD 和已发布提交中的 source closure 字节。
  - 使用独占创建、权限 `0600` 的 claim sidecar；已有 claim 只允许恢复同一 batch 状态。
- `ActiveLearning/batch_runner_v2/al_process.py`
  - 新增固定模块、固定参数的 real-AL 子进程适配器，不接受任意 command/module。
  - 复用 V2-A2 的进程组、超时、reap 与 orphan fail-closed 规则。
  - 校验 worker result 身份、状态和 AL summary 的路径与 SHA-256。
- `ActiveLearning/batch_runner_v2/real_al_child.py`
  - 在子进程内再次验证 manifest、授权、claim、scheduler RUNNING 状态和输入快照。
  - 生产路径调用现有 `run_configured_active_learning()`，要求已有 D0，并注入专用输出根目录。
  - 离线路径只有五种固定 fixture：`success`、`429_success`、`auth_terminal`、
    `terminal`、`uncertain`。
- `ActiveLearning/batch_runner_v2/runner.py`
  - 保持 `run_mock_batch()` 兼容，新增仅供受控调用方使用的 `run_authorized_batch()`。
  - production scope 需要生产授权和显式 `enable_production=True`；offline scope 只能运行固定 fixture。
  - terminal circuit 可在 cleanup 已确认后继续无关任务；uncertain 不自动 replay。
- `tests/test_batch_runner_v2_b2a.py`
  - 新增 13 项 B2A 授权、worker、429、terminal、uncertain、restart、资产和请求上限测试。
- 本报告。

没有修改 AL 选择、acquisition、MMR、performance prediction、D0 sampling 或电路 evaluator
算法。`ActiveLearning/batch_runner_v2/__main__.py` 仍只有 `plan/mock-run/status/report`，
`run_authorized_batch` 也没有从包 `__init__` 导出，因此没有新增通用生产 CLI。

## Real AL invocation contract

父进程只生成固定的：

```text
python -m ActiveLearning.batch_runner_v2.real_al_child \
  --invocation <attempt>/real_al_invocation.json \
  --authorization <exact-authorization.json> \
  --authorization-sha256 <out-of-band-approved-sha256>
```

invocation 精确绑定 batch/job/attempt、manifest path/hash、authorization id/hash、attempt
directory 和可空的离线 fixture。子进程要求目录形状为
`<output>/<batch>/jobs/<job>/attempts/<attempt>`，且状态文件中最后一个 attempt 正处于
`RUNNING`。因此直接手工启动 child 不能绕过 scheduler。

生产调用固定传入：明确的 circuit type/ID、配置路径、已存在 D0、配置与 D0 hash、
`runs_root=<attempt>/al_runs`、`require_existing_d0=True`。运行前后均复核配置、D0、拓扑和
source closure 的 SHA-256。worker 完成只表示 pipeline 完成；`simulation_valid`、
`meets_spec` 与 `stop_reason` 分别记录，不把进程退出 0 当作性能达标。

## Gemini retry ownership 与会计

唯一应用层 retry owner 是 `GeminiRecovery`。生产 transport 使用 OpenAI-compatible Gemini
client 的 `max_retries=0`，避免 SDK 与 V2 双重重试。

- 429：优先采用 `Retry-After` 或 Gemini `retryDelay`，否则使用有界退避。
- 500/502/503/504、网络错误和 timeout：按 `max_attempts` 与 `max_elapsed_seconds` 有界重试。
- 401/403 等认证错误：terminal，不重试。
- 明确 daily/provider quota：写 provider block 并停止后续物理请求。
- 每个物理请求在发送前写 `attempt_started`；重试也计入 batch 共享物理请求上限。
- 缺少 usage 的成功或失败请求均保持 token usage unknown；不按零计费。
- 只承诺可执行的 physical-attempt cap，不声称 token 或金额硬上限。

请求 journal 不保存 prompt 或凭据，只保存 request hash、身份、状态、耗时和可得的 usage。

## 授权机制

`batch_runner_v2b2a_authorization_v1` 必须精确绑定：

- manifest SHA-256、batch ID、source revision 和 source-closure canonical SHA-256；
- provider、model、credential scope；
- 每个 circuit/configuration、config/source/D0/target hashes 与 iteration limit；
- physical-attempt cap、worker timeout 与 retry policy；
- 规范化后的专用 output root；
- execution scope、approver、时间与显式 `approved: true`。

授权文件自己的 SHA-256 必须由独立可信渠道传给启动器；文件内自报 hash 不构成授权。
一份授权只能 claim 到一个固定 batch/output root。同一 claim 只有在相同 `status.json` 已存在时
可用于重启；不能转移到别的输出目录或新 batch。

这套机制是代码侧 fail-closed 边界，不是签名基础设施，也不会自行证明审批人身份。生产前仍需
人工/组织审批，并由可信 launcher 固定授权 digest；不得把本地生成的 JSON 当成正式批准。

## Release source closure

当前 schema 强制的最小运行 closure 及本次工作树 SHA-256 如下。生产 manifest 必须从最终审查、
已提交的新 release commit 重新计算，不能复制这里的工作树 hash 冒充 release identity。

| 文件 | SHA-256（当前工作树） |
|---|---|
| `ActiveLearning/main.py` | `703b0a82c4976e47cfb805d8b0a9d87d5f856a80a43630bc9088a0749b6b02e3` |
| `ActiveLearning/active_learning.py` | `170f999cae9ce1837aa92f5bf942aa0c31f783b98729aa3effe9cdd5e19c9e2e` |
| `ActiveLearning/experiment_logging.py` | `32fd934d313960fa871b379879ead8a0c6c33abc441e9e56945c0cbff4a9a256` |
| `ActiveLearning/inverse_design/llm_client.py` | `37b3e5ce74dbf77ad5b59d7b3c6893b11ed72d8e2bbb2eda22276c75cdf5d4bc` |
| `ActiveLearning/batch_runner_v2/al_process.py` | `1fc74cdf284d016d5fd2a08115a0ccb6a49b19d1acc83970f13c1f6d52b1ab6f` |
| `ActiveLearning/batch_runner_v2/authorization.py` | `3b383b68b783436633296ee9f6937be1ec41b249af924f69ce9f6512204b1fb9` |
| `ActiveLearning/batch_runner_v2/gemini_recovery.py` | `0e2661d32d20dda7ee6231348a6e0c925f2b4a5c6e2d3fb02e291bff6d9008b5` |
| `ActiveLearning/batch_runner_v2/llm_bridge.py` | `f091cd733ef607dd57ffd754a8ec642968762823e9bfdc23a6b9b43f3b262c51` |
| `ActiveLearning/batch_runner_v2/real_al_child.py` | `24bf6ffbbaf9c367ad2541826b8b7e05bdb0534368a9058f5c2aef4adb15a6cc` |
| `ActiveLearning/batch_runner_v2/runner.py` | `d6e16510641f443c758c57eb0ffdb93bf6b771fdc86900209c86746ebf648a9b` |
| `ActiveLearning/batch_runner_v2/schema.py` | `e88411203c7384557f913aa1361dda1b46c5fa9ea34067f4b9b2a8f866048e90` |
| `ActiveLearning/batch_runner_v2/storage.py` | `ffb81925908c574bdf7c7ced331d5fceeabbe0bfb1b850a2f8616ac42716d932` |

这只是最小边界，不是首个电路的完整 Python import closure。首个 production manifest 还必须纳入
实际会加载的 onboarding、acquisition、data expansion、inverse design、performance prediction、
real evaluation、validity、evaluator/validator、prompt/template 及 Stage A/B 依赖。当前工作树这些区域含
混合 Book625/AG626 与其他未提交修改，不能作为冻结 V2-B1 的隐含能力发布。

## 离线测试结果

最终 B2A 代码对应结果：

- V2 全套：`59 tests passed`（原 V2-B1 46 + B2A 13），无失败。
- AL core mocked regression：`51 tests passed`，无失败。
- Book625、AG626、CurrentMirror 定向 pytest：`51 passed`，无失败。

B2A 端到端覆盖 scheduler 启动 worker、现有 D0/config 加载、metrics/result 记录、真实 bridge
边界上的 429 -> success、auth terminal、确认 cleanup 后继续、uncertain 不重放、restart 跳过完成、
资产/hash 预检、physical cap 和 production-disabled gate。所有测试均移除了 Gemini/OpenAI key，
没有外部请求或真实 SPICE。

## 首个 Ubuntu real smoke 候选（仅提案）

建议：`CurrentMirror/circuit_0469`。它没有 AG626 的一次性 claim，配置和 20 条独立 D0 已存在，
且当前 AL runtime 能解析其标准路径。

| 资产 | SHA-256 |
|---|---|
| `ActiveLearning/InitializeD0/examples/CurrentMirror/circuit_0469/active_learning_config.yaml` | `612c73f32682a17cdce98fb488731ab88df1f782580c61d898b0ee4b7b956ac6` |
| `ActiveLearning/InitializeD0/examples/CurrentMirror/circuit_0469/d0/d0_v3_seed42.jsonl` | `945c03209c038e9a1b97929093a660a9d5bffc51e3637dfcb1c97545f19b86fe` |
| `dataset/circuit_0469/topology/circuit_0469.py` | `3e0095a5fd17e5622c9519bf6b5f03e1b5b1a5aa7b46cc4b1e4bd9208005d6f5` |
| `ActiveLearning/InitializeD0/examples/CurrentMirror/circuit_0469/parameter_template.yaml` | `02b4b5c6e3553c49a1d89d1a7a2347c004edfa925fe30aa56a5e4caf8576ba3c` |
| `ActiveLearning/InitializeD0/examples/CurrentMirror/circuit_0469/simulation/parameter/ideal_param.yaml` | `7ec6dc7bc7d7023f6f93610ae4298ff323338feacae416643c9e12ce92e4b435` |
| `ActiveLearning/circuit_types/CurrentMirror/performance_template.yaml` | `e629cfdbefd0e1e116e5d7bbaa04710c9f5dfe1f3705336edeeacad9b52135f3` |

配置 identity fallback 是 `sha256-612c73f32682a17c`；target spec canonical SHA-256 为
`1ef24de5f1c8272fb93579ceb2f6340abde7f41fc61902ce613f81035d3a7a15`。目标保持
`current_error <= 0.04`、`output_resistance >= 26000000`，最大迭代 5。D0 有 20 条、20 条
simulation-valid；历史记录中 1 条旧 `meets_spec=true` 同时 functional-infeasible，按当前正式 gate
重新判断没有初始可接受解，不能把该旧字段当成 smoke 已成功。

现有算法每迭代最多执行一次 inverse-design LLM 和一次 performance-prediction LLM，因此 5 轮的
规划上界为 10 个 logical LLM calls、5 个 SPICE evaluations；若批准 `max_attempts=8`，理论最坏为
80 个 physical requests。首次 smoke 应由审批者明确选择更小的 physical cap/retry policy；达到 cap
只会 BLOCKED，不会放宽限制。token/金额仍无法预先硬界定。

没有选择 Book625 0489（其配置明确绑定本地 OpenAI-compatible provider），没有选择 AG626 0490
（一次性授权/guard），也没有选择已有初始可行解、可能直接 early stop 的 OpAmp 候选。

## 剩余阻塞与 Ubuntu rollout

生产前必须全部完成：

1. 从当前混合工作树选择性整理真实 AL dependency closure；逐文件审查后提交到新的固定 release
   commit。不得把未提交的 Book625/AG626 或其他电路更改静默带入。
2. 在干净 detached Ubuntu worktree 中复跑 V2、AL mocked 和目标电路离线检查，并验证 Python、
   PySpice/ngspice 与 dataset 映射。Windows 结果不能替代 Ubuntu 证据。
3. 以该新 commit 和 Ubuntu 实际只读资产重新计算完整 source closure、config/D0/topology/target
   hashes，生成 candidate manifest；先运行 `plan`，不得启动生产。
4. 独立审核 manifest、closure、模型、credential scope、输出目录、迭代、worker timeout、
   physical cap 和 retry policy。审核系统生成/批准 authorization，并通过独立渠道固定其 SHA-256。
5. 保护 authorization 与 claim 所在目录，仅允许运行账户写入；确认专用 output root 不存在或仅含
   同一 V2 状态，绝不指向历史实验目录。
6. 先跑 `offline_integration` 三任务 smoke；验证 429、terminal、restart、journal 与无孤儿进程。
7. 只有另一次明确人工生产批准后，可信 launcher 才可调用未公开的 programmatic production path；
   不新增通用 production CLI。一次只运行同一 credential scope 的一个 batch，避免跨 batch cap/cooldown
   并发竞争。

当前 production blockers：未发布的新 commit、未完成的全量动态依赖 closure、未取得外部生产授权、
未完成 Ubuntu real environment/preflight，以及未批准首个 smoke 的物理请求预算。这些 blocker 不影响
B2A 的离线集成就绪结论。
