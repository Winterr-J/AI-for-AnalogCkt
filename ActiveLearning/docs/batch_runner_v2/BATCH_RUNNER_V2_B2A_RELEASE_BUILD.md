# Batch Runner V2-B2A Release Build

状态：V2_B2A_RELEASE_BUILD_READY

本报告记录从固定基线构造的未提交 release candidate。它只表示候选可以进入人工 Git 审核和
Commit 阶段，不表示已获得 production AL、Gemini、SPICE 或 Ubuntu 部署批准。

## 1. Release worktree

- Worktree：D:\analogCkt\AG_simulation_v2_b2a_rc
- Branch：codex/batch-runner-v2-b2a-rc
- Baseline / current HEAD：e0276463b179b161dd63819a9107bda9bcf782e2
- 工作树保持未暂存、未提交；未执行 git add、commit、push、SSH 或部署。
- 原研究工作树和 V2-B1 worktree 未修改。

## 2. generate_closure.py 根因与取证

缺失条目位于原始 manifest 的 source_files 类别：

- 路径：generate_closure.py
- 记录 SHA-256：913c1670a2e83b82500ed042289387df46d5f9e5e26365a06e0eaf928c95ced2

取证结果：

1. 对 production source 和 tests 的引用搜索为零；运行时、测试和 release verifier 均不导入该文件。
2. git log --all --full-history、git rev-list --all --objects 均没有该路径。
3. 基线工作树、V2-B1 worktree、当前 RC 及已知临时构造目录均无同名文件。
4. 已批准 staging 列表从未包含它；B2A release closure 文档说明 manifest 是在临时 detached
   构造环境中生成，而不是由该脚本作为发布运行资产提供。
5. 原生成辅助脚本在仓库根枚举已加载本地 Python 文件时把自身计入 source_files；临时 worktree
   删除后，自引用条目留下，但脚本本身不是候选源、运行依赖或测试依赖。

因此没有可验证的 authentic copy，且不得依据已知 hash 反向伪造文件。该文件是一次性的 manifest
生成辅助工具；闭包可使用标准库 JSON/SHA-256 verifier 完整复核，不依赖该脚本。

## 3. 原始与修正版 manifest

原始审计 artifact 保持字节不变：

- ActiveLearning/docs/batch_runner_v2/BATCH_RUNNER_V2_B2A_SOURCE_CLOSURE.json
- SHA-256：ad74cbbbc3e4f87e0d9b37980de11b0538b52baf47e09365ad34fa5270a5974d
- 条目：115 source + 8 runtime assets + 4 tests = 127
- 已知 unsupported entry：source_files/generate_closure.py

显式版本化修正版：

- ActiveLearning/docs/batch_runner_v2/BATCH_RUNNER_V2_B2A_SOURCE_CLOSURE_V2.json
- SHA-256：b9f4550332f35f66c6e9d4ed8d4f659c57d41ff4a12b568d975af3dde008e8b9
- 条目：114 source + 8 runtime assets + 4 tests = 126
- 与原始 JSON 的语义差异只有删除 source_files/generate_closure.py；base_revision、
  candidate_revision、external_runtime、generation_note、其余路径及其期望 hash 全部不变。

原始 manifest 仅作为审计证据保留。后续 release verification 和 production closure 构造必须明确使用
SOURCE_CLOSURE_V2.json；不得再把原始 127 条清单当作通过证据。

## 4. 独立 closure verifier

以下 verifier 不依赖 generate_closure.py，并明确读取修正版 manifest：

~~~powershell
$rc = 'D:\analogCkt\AG_simulation_v2_b2a_rc'
$manifest = Join-Path $rc 'ActiveLearning\docs\batch_runner_v2\BATCH_RUNNER_V2_B2A_SOURCE_CLOSURE_V2.json'
$python = 'C:\Users\clair\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$code = @'
import hashlib, json, pathlib, sys
root = pathlib.Path(sys.argv[1]).resolve()
manifest_path = pathlib.Path(sys.argv[2]).resolve()
data = json.loads(manifest_path.read_text(encoding="utf-8"))
missing, mismatched = [], []
for category in ("source_files", "runtime_assets", "test_files"):
    for relative, expected in data[category].items():
        path = root / relative
        if not path.is_file():
            missing.append((category, relative))
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            mismatched.append((category, relative, actual, expected))
count = sum(len(data[name]) for name in ("source_files", "runtime_assets", "test_files"))
print({"entries": count, "missing": missing, "mismatched": mismatched})
raise SystemExit(count != 126 or bool(missing) or bool(mismatched))
'@
& $python -c $code $rc $manifest
~~~

本轮执行结果：126/126，missing=[]，mismatched=[]。测试完成后再次复核仍为 126/126。

## 5. 重建内容与 patch

纳入候选的 runtime/source 变化：

- ActiveLearning/main.py
- ActiveLearning/batch_runner_v2/al_process.py
- ActiveLearning/batch_runner_v2/authorization.py
- ActiveLearning/batch_runner_v2/real_al_child.py
- ActiveLearning/batch_runner_v2/runner.py
- ActiveLearning/batch_runner_v2/schema.py
- tests/test_batch_runner_v2_b2a.py

main.py 只应用 BATCH_RUNNER_V2_B2A_MINIMAL_MAIN.patch；没有从脏研究工作树整文件覆盖。
Patch 对基线 apply --check 和实际应用均成功。没有引入 Book625、AG626、Comparator、Filter、
VoltageReference 或其他研究改动。

## 6. Runtime assets

八个 runtime assets 均由固定 baseline Git tree 提供，并在修正版 manifest 中逐字节匹配：

- CurrentMirror circuit_0469 config
- d0_v3_seed42.jsonl
- ideal_param.yaml
- CurrentMirror performance template
- generic/current-mirror inverse-design prompts
- generic/current-mirror performance-prediction prompts

没有复制历史实验 output、real-SPICE raw/profiling 结果或额外 D0。D0 只作为既有受哈希约束的
runtime asset；本轮未生成或修改 D0。

## 7. 敏感文件与授权排除

候选不含 API key、环境 secret、private key、已消费 approval/claim、batch output 或历史执行记录。
测试前移除 GEMINI_API_KEY、GOOGLE_API_KEY、OPENAI_API_KEY；检查结果
api_credentials_present=False。未创建 production authorization/claim，也未调用
enable_production=True。

## 8. 离线验证

使用 Python 3.12.14。PYTHONPATH 的第一项是 RC；第二项仅为既有第三方依赖目录
D:\analogCkt\AG_simulation\_codex_test_deps。该依赖目录不含 ActiveLearning 源码，运行后检查所有
已加载 ActiveLearning.* 均来自 RC。

结果：

| 检查 | 结果 |
|---|---|
| 修正版 closure | 126/126 PASS；0 missing，0 mismatch |
| V2 test_batch_runner_v2*.py | 59/59 PASS，0 fail，0 skip，24.874 s |
| Mock-only AL/inverse-design/CurrentMirror | 34/34 PASS，0 fail，0 skip，0.081 s |
| Python in-memory compile | 118 个 manifest Python 文件 PASS |
| ActiveLearning import provenance | PASS；错误来源集合为空 |
| git diff --check | PASS；仅 LF→CRLF 提示，无 whitespace error |
| API credential check | PASS；三个 key 均不存在 |
| nominal evaluator | 明确未运行 |
| real Gemini/LLM、SPICE、D0、Profiling、production AL | 均未运行 |

首次只把 RC 放进 PYTHONPATH 时，V2 得到 58 PASS + 1 import ERROR；错误发生在 mock patch 建立前，
底层原因是 ActiveLearning.main 所需第三方依赖未加入解释器路径，不是候选代码失败。加入批准的
dependency-only 目录后完整 59 项通过。不能把首次运行记作通过，也不能省略 release 环境依赖路径。

34 项选择集明确排除
test_circuit_0469_nominal_regression；两个 evaluator error-path 测试 patch 了 ngspice 配置并使用 fake
circuit，其余三项只检查 adapter/参数错误。没有测试意外进入真实 ngspice 或网络调用。

## 9. Git diff 与精确 staging 范围

当前 tracked diff：4 files changed，376 insertions，39 deletions。另有 9 个批准的 untracked 文件；
无 deletion。精确 staging 列表共 13 个：

1. ActiveLearning/main.py
2. ActiveLearning/batch_runner_v2/al_process.py
3. ActiveLearning/batch_runner_v2/authorization.py
4. ActiveLearning/batch_runner_v2/real_al_child.py
5. ActiveLearning/batch_runner_v2/runner.py
6. ActiveLearning/batch_runner_v2/schema.py
7. tests/test_batch_runner_v2_b2a.py
8. ActiveLearning/docs/batch_runner_v2/BATCH_RUNNER_V2_B2A_MINIMAL_MAIN.patch
9. ActiveLearning/docs/batch_runner_v2/BATCH_RUNNER_V2_B2A_REAL_AL_INTEGRATION.md
10. ActiveLearning/docs/batch_runner_v2/BATCH_RUNNER_V2_B2A_RELEASE_CLOSURE.md
11. ActiveLearning/docs/batch_runner_v2/BATCH_RUNNER_V2_B2A_SOURCE_CLOSURE.json
12. ActiveLearning/docs/batch_runner_v2/BATCH_RUNNER_V2_B2A_SOURCE_CLOSURE_V2.json
13. ActiveLearning/docs/batch_runner_v2/BATCH_RUNNER_V2_B2A_RELEASE_BUILD.md

RELEASE_CLOSURE.md 与原始 manifest 保留为生成时审计 artifact；其中 127/127 的历史表述已被本报告
和 V2 manifest 明确纠正，不能作为当前 verifier 输入。

## 10. 人工 Commit / Push 步骤

本轮没有执行以下命令。人工批准后逐路径暂存：

~~~powershell
$rc = 'D:\analogCkt\AG_simulation_v2_b2a_rc'
git -C $rc add -- ActiveLearning/main.py
git -C $rc add -- ActiveLearning/batch_runner_v2/al_process.py
git -C $rc add -- ActiveLearning/batch_runner_v2/authorization.py
git -C $rc add -- ActiveLearning/batch_runner_v2/real_al_child.py
git -C $rc add -- ActiveLearning/batch_runner_v2/runner.py
git -C $rc add -- ActiveLearning/batch_runner_v2/schema.py
git -C $rc add -- tests/test_batch_runner_v2_b2a.py
git -C $rc add -- ActiveLearning/docs/batch_runner_v2/BATCH_RUNNER_V2_B2A_MINIMAL_MAIN.patch
git -C $rc add -- ActiveLearning/docs/batch_runner_v2/BATCH_RUNNER_V2_B2A_REAL_AL_INTEGRATION.md
git -C $rc add -- ActiveLearning/docs/batch_runner_v2/BATCH_RUNNER_V2_B2A_RELEASE_CLOSURE.md
git -C $rc add -- ActiveLearning/docs/batch_runner_v2/BATCH_RUNNER_V2_B2A_SOURCE_CLOSURE.json
git -C $rc add -- ActiveLearning/docs/batch_runner_v2/BATCH_RUNNER_V2_B2A_SOURCE_CLOSURE_V2.json
git -C $rc add -- ActiveLearning/docs/batch_runner_v2/BATCH_RUNNER_V2_B2A_RELEASE_BUILD.md
git -C $rc diff --cached --check
git -C $rc diff --cached --name-status
git -C $rc commit -m "Integrate Batch Runner V2 B2A release candidate"
~~~

人工复核 commit 后才可执行：

~~~powershell
git -C $rc push -u origin codex/batch-runner-v2-b2a-rc
~~~

## 11. Clean-SHA retest

Commit 后不能沿用当前未提交结果冒充 fixed-SHA evidence：

~~~powershell
$rc = 'D:\analogCkt\AG_simulation_v2_b2a_rc'
$sha = git -C $rc rev-parse HEAD
git -C $rc status --porcelain
git -C $rc diff --name-status e0276463b179b161dd63819a9107bda9bcf782e2 $sha
git -C $rc worktree add --detach D:\analogCkt\AG_simulation_v2_b2a_sha_check $sha
~~~

在 clean detached checkout 中重新执行本报告第 4 节 verifier、118 文件内存编译、59 项 V2 和 34 项
Mock-only 回归；确认 status 为空、ActiveLearning import provenance 只指向该 checkout。把真实
release SHA 写入外部 release record 和之后的 batch manifest；不要尝试把 manifest 内的
candidate_revision=null 改成包含该 manifest 的同一 commit SHA，避免自引用。

## 12. Ubuntu offline rollout 与剩余 blocker

Commit、push 和 clean-SHA retest 经人工批准后，Ubuntu 只能在独立 checkout 中取精确 SHA，先运行
只读 HEAD/status/hash/provenance 检查，再运行无凭据的 closure/compile/59+34 offline checks、
Linux process-group/orphan 检查和 Fake Gemini/Fake SPICE 三任务批次。不得修改旧部署。

仍需单独批准或补证：

1. Ubuntu Python/PySpice/ngspice executable/shared-library 兼容性；
2. Linux process isolation/orphan 实机证据；
3. circuit_0469 D0 provenance 的独立 initialization manifest/CSV；
4. Ubuntu nominal evaluator（本轮禁止且未运行）；
5. 新 batch 的 authorization/claim、生产凭据和显式 production approval；
6. clean commit SHA 的重新验证。

结论：closure 自引用已按最小范围纠正，候选可进入人工 Commit 审核阶段；尚不可运行真实 AL 或部署。
