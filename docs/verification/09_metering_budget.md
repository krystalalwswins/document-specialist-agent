# P1-3 Token、时延、成本计量与预算验证记录

> 实现分支：`feat/harness-p1-3`
> 开发基线：`feat/harness-p1-2`
> 当前状态：实现与学习文档已提交；按任务约定，本轮未执行离线测试或外部验证。

## 1. 验收对象

| 项目 | 验收后填写 |
| --- | --- |
| Commit SHA | `PENDING` |
| 工作区状态 | `PENDING` |
| 操作系统 / Python | `PENDING` |
| 测试模型 | `PENDING` |
| 价格表版本 | `PENDING` |
| 是否调用真实模型 | `NO`（除非另行授权） |

## 2. 证据矩阵

| 能力 | 直接证据 | 核心断言 |
| --- | --- | --- |
| usage 规范化 | `test_usage_normalizes_openai_cached_tokens_and_response_model` | OpenAI 缓存字段和响应实际模型写入事件 |
| DeepSeek 缓存字段 | `test_usage_normalizes_deepseek_cache_hits_and_derives_total` | cache hit 统一为 cache_tokens，缺失 total 可确定性求和 |
| 三层聚合与估价 | `test_usage_is_aggregated_by_task_phase_and_iteration_with_cache_pricing` | task/phase/iteration Token、时延、重试与缓存价格正确 |
| 未知价格 | `test_unknown_model_keeps_tokens_but_marks_cost_unavailable` | Token 保留，费用为 null，不虚构零成本 |
| soft/hard budget | `test_soft_budget_requests_convergence_and_hard_budget_raises_once` | soft 发一次收敛事件，hard 抛明确异常 |
| 记忆阶段边界 | `test_memory_capture_is_measured_but_not_charged_to_agent_loop_budget` | 记忆提取计量但不消耗核心循环预算 |
| 上下文收敛接线 | `test_soft_budget_signal_forces_context_manager_convergence_path` | soft 信号进入 ContextManager 并保留安全消息组 |
| 配置约束 | `test_task_token_budget_requires_soft_below_hard` / `test_pricing_configuration_is_all_or_none` | soft < hard；价格版本和三档费率全有或全无 |

## 3. 建议验收命令（本轮未执行）

Windows PowerShell：

```powershell
.venv\Scripts\python.exe -m pytest -q tests/test_metering.py tests/test_llm_client.py tests/test_config.py
.venv\Scripts\python.exe -m pytest -q
```

Linux / macOS：

```bash
.venv/bin/python -m pytest -q tests/test_metering.py tests/test_llm_client.py tests/test_config.py
.venv/bin/python -m pytest -q
```

这组验收全部可以使用 fake，不需要 Docker、MinIO 或 API Key。真实供应商账单对账不是
P1-3 自动化测试的一部分；若要抽样对账，必须固定模型、价格表版本和供应商账单周期。

## 4. 必须检查

- 每个成功 LLM 事件包含 provider 能提供的 prompt/completion/total/cache tokens；
- 重试 attempt 计入 attempts 和时延，不在无 usage 时增加 Token；
- `metrics.usage` 同时包含 task、by_phase、by_iteration；
- plan、replan、context_compaction、execute、memory_capture 阶段不会混写；
- 缓存 Token 不会同时按普通输入和缓存输入重复计价；
- 未配置价格或未知模型时费用必须是 null；
- `cost_kind` 明确是 estimate，不得在文档或简历中称为供应商最终账单；
- soft budget 只触发收敛，不直接失败；
- hard budget 产生明确错误、任务失败并保留 usage/budget_events；
- 价格表版本和三档费率必须一起修改并留存。

## 5. 结果（验收后填写）

- 聚焦 pytest：`PENDING`
- 完整 pytest：`PENDING`
- Fake Evaluation：`NOT RUN`（本任务不要求）
- 真实模型 / Docker / MinIO：`NOT RUN`

完整输出：

```text
PENDING
```

## 6. 收口规则

只有实际执行者填写准确 SHA、环境和原始输出后，才能把 PENDING 改成 PASS。费用公式单元
测试通过只证明本地计算正确，不证明价格表仍是供应商当前价格；每次对外展示估算费用时
必须同时展示 model、pricing_version 和 `estimate_not_provider_bill` 语义。
