# P1-3 Token、时延、成本计量与预算验证记录

> 实现分支：`feat/harness-p1-3`
> 开发基线：`feat/harness-p1-2`
> 当前状态：离线验收已于 2026-09-28 在 `main` 上执行并通过；真实模型与外部服务未调用。
> 证据见第 5 节。

## 1. 验收对象

| 项目 | 验收结果 |
| --- | --- |
| Commit SHA | `b9bbe72c57831572364c2ac8bfa7a62229514189`（分支 `main`） |
| 工作区状态 | 执行测试时为空；随后仅新增本验收记录文件 |
| 操作系统 / Python | Windows 11 25H2（build 26200）/ 3.13.2（`.venv`），pytest 9.1.1 |
| 测试模型 | Fake LLM；`Settings.llm_model` 仅作为配置默认值断言，未发起真实调用 |
| 价格表版本 | `unconfigured`（未配置 `LLM_PRICING_VERSION` 与三档费率） |
| 是否调用真实模型 | `NO` |

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

## 5. 结果

- 聚焦 pytest：`20 passed in 1.72s`
- 完整 pytest：`364 passed, 1 skipped, 1 warning in 11.95s`
- Fake Evaluation：`PASS`（本次另行执行，`run_id = b146f97c4cb849a1857ee6487165a346`）
- 真实模型 / Docker / MinIO：`NOT RUN`

完整输出：

```text
$ .venv\Scripts\python.exe -m pytest -q tests/test_metering.py tests/test_llm_client.py tests/test_config.py
....................                                                     [100%]
20 passed in 1.72s
```

### 5.1 验收起始时的阻塞（必须记录）

第 3 节给出的聚焦命令在验收开始时**无法执行**：

```text
tests/test_metering.py:7: in <module>
    from context.manager import ContextManager, ContextSnapshot
context\__init__.py:10: in <module>
    from .compactor import ContextCompactor, ContextSummary
context\compactor.py:12: in <module>
    from agent.llm_client import LLMClient
agent\executor.py:20: in <module>
    from context.manager import ContextManager, ContextSnapshot
E   ImportError: cannot import name 'SUMMARY_MARKER' from partially initialized module 'context.compactor'
```

即 `context` ↔ `agent` 的顺序相关循环导入：只有把 `context` 作为首个导入的入口才会触发，
因此完整回归（`test_api.py` 先收集）反而掩盖了它。最小修复为把
`context/compactor.py` 的 `LLMClient` 改为 `TYPE_CHECKING` 类型期导入（提交 `b9bbe72`），
不改变运行时行为与公开 API。修复后 19 个顶层入口分别作为首个导入的冒烟检查
`failures=0`。

### 5.2 第 4 节必查行为对照

- 事件包含 provider 提供的 prompt/completion/total/cache tokens：由
  `test_llm_client.py` 的 `test_usage_normalizes_*` 覆盖；
- 重试 attempts/时延计量、无 usage 时不虚增 Token：由 `test_metering.py` 覆盖；
- `metrics.usage` 含 task / by_phase / by_iteration：由
  `test_usage_is_aggregated_by_task_phase_and_iteration_with_cache_pricing` 覆盖；
- 阶段不混写、缓存 Token 不重复计价、未知模型费用为 `null`：由
  `test_unknown_model_keeps_tokens_but_marks_cost_unavailable` 与聚合用例覆盖；
- soft 只触发收敛、hard 抛明确异常：由
  `test_soft_budget_requests_convergence_and_hard_budget_raises_once` 覆盖；
- 记忆阶段计量但不计入核心循环预算：由
  `test_memory_capture_is_measured_but_not_charged_to_agent_loop_budget` 覆盖；
- 配置约束（soft < hard、价格版本与三档费率全有或全无）：由 `test_config.py` 覆盖。

本次结果全部来自 Fake LLM 与本地价格计算。按第 6 节，费用字段仅代表本地估算，
已验证的价格表版本为 `unconfigured`（即不产生费用数字），因此本次**没有**任何可对外
展示的费用数值，也就不存在"把估算说成供应商账单"的风险。

## 6. 收口规则

本页已于 2026-09-28 由实际执行者填写准确 SHA、环境和原始输出，离线结论为通过
（真实模型 / Docker / MinIO 为 `NOT RUN`）。费用公式单元测试通过只证明本地计算正确，
不证明价格表仍是供应商当前价格；每次对外展示估算费用时必须同时展示 model、
pricing_version 和 `estimate_not_provider_bill` 语义。
