# P1-2 独立 Evaluation 验证记录

> 实现分支：`feat/harness-p1-2`
> 开发基线：`feat/harness-p1-1`
> 当前状态：实现与学习文档已提交；按任务约定，本轮未执行离线或真实评测。

## 1. 验收对象

| 项目 | 验收后填写 |
| --- | --- |
| Commit SHA | `PENDING` |
| 工作区状态 | `PENDING` |
| 操作系统 / Python | `PENDING` |
| 数据集 ID / 版本 | `document-specialist-harness-core` / `1.0.0` |
| Prompt 版本 | `PENDING` |
| Fake 模型 | `scripted-fake` |
| 真实模型 | `PENDING` |

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

## 5. 结果（验收后填写）

- 聚焦 pytest：`PENDING`
- Fake Evaluation：`PENDING`
- 完整 pytest：`PENDING`
- 真实模型 Evaluation：`NOT RUN`（除非另行明确授权）

完整输出：

```text
PENDING
```

## 6. 收口规则

只有实际执行者填写准确 SHA、环境和原始输出后，才能把这里的 PENDING 改成 PASS。
真实模型评测不是 P1-2 代码交付的强制条件，但若简历声称具体真实模型效果，必须附上
对应模型、Prompt、数据集版本和报告，不得用 Fake 结果替代。
