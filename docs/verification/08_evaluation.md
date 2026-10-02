# P1-2 独立 Evaluation 验证记录

> 实现分支：`feat/harness-p1-2`
> 开发基线：`feat/harness-p1-1`
> 当前状态：Fake 模式离线评测已于 2026-09-28 在 `main` 上执行并通过；真实模式评测
> 未执行（需显式授权）。证据见第 5 节。

## 1. 验收对象

| 项目 | 验收结果 |
| --- | --- |
| Commit SHA | `b9bbe72c57831572364c2ac8bfa7a62229514189`（分支 `main`） |
| 工作区状态 | 执行测试时为空；随后仅新增本验收记录文件 |
| 操作系统 / Python | Windows 11 25H2（build 26200）/ 3.13.2（`.venv`），pytest 9.1.1 |
| 数据集 ID / 版本 | `document-specialist-harness-core` / `1.0.0` |
| Prompt 版本 | `harness-prompts-v1`（默认值，未覆盖） |
| Fake 模型 | `scripted-fake` |
| 真实模型 | 未调用（`NOT RUN`，未使用 `--allow-external`） |

## 2. 证据矩阵

| 能力 | 直接证据 | 核心断言 |
| --- | --- | --- |
| 数据集版本化 | `test_default_dataset_is_versioned_and_covers_nine_required_categories` | 九类案例、唯一 ID、版本固定 |
| 轨迹评分 | `test_scorer_uses_task_trace_instead_of_model_self_judgement` | 工具、步骤、Token 从 Task 读取 |
| 完整 Fake Harness | `test_fixed_fake_suite_runs_through_the_harness_and_produces_all_metrics` | 九案例走 Orchestrator 并输出九项指标 |
| 报告留存 | `test_reporter_writes_versioned_json_and_markdown` | JSON/Markdown 含模型、Prompt、数据集版本 |
| 真实模式安全门 | `test_real_mode_requires_explicit_external_opt_in` | 未授权时不调用外部服务 |

## 3. 建议验收命令（本轮未执行）

Windows PowerShell：

```powershell
.venv\Scripts\python.exe -m pytest -q tests/test_evaluation.py
.venv\Scripts\python.exe -m evaluation --mode fake
.venv\Scripts\python.exe -m pytest -q
```

Linux / macOS：

```bash
.venv/bin/python -m pytest -q tests/test_evaluation.py
.venv/bin/python -m evaluation --mode fake
.venv/bin/python -m pytest -q
```

真实模型评测必须单独执行，且运行前确认 API Key、Docker、MinIO 和数据集输入：

```powershell
.venv\Scripts\python.exe -m evaluation --mode real --allow-external `
  --prompt-version harness-prompts-v1
```

## 4. 必须检查

- Fake 报告包含九个案例，且 JSON/Markdown 能正常打开；
- 文档解析、统计、产物、缺失数据、参数错误、瞬态故障、大输出、重规划、记忆召回均有独立案例；
- 瞬态故障必须出现 RETRYING 轨迹后恢复，不能直接脚本化成功；
- 参数错误必须先产生 FAILED TaskStep，再以修正参数成功；
- 大输出必须出现 `context_output_offloaded` 并调用 `read_tool_output`；
- 产物案例必须存在成功 `artifact_check`；
- 真实模式未加 `--allow-external` 时必须在构建 Orchestrator 前终止；
- 报告记录 mode、model、prompt_version、dataset_version 和 run_id；
- Fake 高分不得被描述为真实模型效果。

## 5. 结果

- 聚焦 pytest：`5 passed in 1.78s`
- Fake Evaluation：`PASS`，九条案例全部通过，`run_id = b146f97c4cb849a1857ee6487165a346`
- 完整 pytest：`364 passed, 1 skipped, 1 warning in 11.95s`
- 真实模型 Evaluation：`NOT RUN`（未显式授权）

完整输出：

```text
$ .venv\Scripts\python.exe -m pytest -q tests/test_evaluation.py
.....                                                                    [100%]
5 passed in 1.78s

$ .venv\Scripts\python.exe -m evaluation --mode fake
evaluation=PASS
json_report=.data\evaluation\reports\document-specialist-harness-core-fake-b146f97c4cb849a1857ee6487165a346.json
markdown_report=.data\evaluation\reports\document-specialist-harness-core-fake-b146f97c4cb849a1857ee6487165a346.md
```

报告主体（`.data/evaluation/reports/...b146f97c....md`，该目录被 `.gitignore` 忽略，
故在此留档关键内容）：

```text
# Evaluation Report: document-specialist-harness-core
- Run: b146f97c4cb849a1857ee6487165a346
- Mode / model: fake / scripted-fake
- Dataset / prompt: 1.0.0 / harness-prompts-v1
- Result: PASS

| Metric | Value |
| Case pass rate | 100.00% |
| Task completion rate | 100.00% |
| Tool selection accuracy | 100.00% |
| Plan step completion rate | 100.00% |
| Artifact validation pass rate | 100.00% |
| Average tool calls | 1.11 |
| Average replans | 0.22 |
| Failure recovery rate | 100.00% |
| Average tokens | 333.33 |
| P95 latency | 11.87 ms |

| Case | Category | Result | Tools | Replans | Tokens |
| document-parsing | document_parsing | PASS | 1 | 0 | 300 |
| structured-statistics | structured_data_statistics | PASS | 1 | 0 | 300 |
| artifact-generation | artifact_generation | PASS | 1 | 0 | 300 |
| missing-data | missing_data | PASS | 0 | 1 | 300 |
| tool-parameter-error | tool_parameter_error | PASS | 2 | 0 | 400 |
| transient-tool-failure | transient_tool_failure | PASS | 1 | 0 | 300 |
| large-tool-output | large_tool_output | PASS | 2 | 0 | 400 |
| local-replanning | local_replanning | PASS | 2 | 1 | 500 |
| memory-recall | memory_recall | PASS | 0 | 0 | 200 |
```

第 4 节必查行为中的"九类案例覆盖""瞬态故障 RETRYING 后恢复""参数错误先 FAILED 再
修正成功""大输出出现 `context_output_offloaded` 并调用 `read_tool_output`"
"真实模式未加 `--allow-external` 时在构建 Orchestrator 前终止"
"报告记录 mode/model/prompt_version/dataset_version/run_id"均已由
`tests/test_evaluation.py` 的 5 个用例断言覆盖。

> 上述全部为 **Fake 模型**结果，只证明 Harness 调用链与评分/报告链路可用，
> **不能**作为任何真实模型效果的证据。

## 6. 收口规则

本页已于 2026-09-28 由实际执行者填写准确 SHA、环境和原始输出，Fake 模式结论为
`PASS`；真实模型仍为 `NOT RUN`。
真实模型评测不是 P1-2 代码交付的强制条件，但若简历声称具体真实模型效果，必须附上
对应模型、Prompt、数据集版本和报告，不得用 Fake 结果替代。
