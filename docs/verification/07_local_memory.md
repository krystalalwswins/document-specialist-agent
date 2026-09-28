# P1-1 本地长期记忆验收说明

> 记录日期：2026-09-23  
> 实现分支：`feat/harness-p1-1`  
> 开发基线：`feat/harness-p0-5` / `d5be978`（远端）  
> 当前状态：已于 2026-09-28 在 `main` 上完成离线验收并通过（聚焦与全量均通过）。
> 证据见第 5 节。

## 1. 验收范围

本编号只验证 SQLite 非向量长期记忆，不引入 PostgreSQL、pgvector、Embedding、MCP、
Evaluation 或多 Agent。

## 2. 测试证据

| 能力 | 测试文件 | 关键断言 |
| --- | --- | --- |
| SQLite 持久化/去重 | `test_memory_store.py` | 重建 Store 后可读；ACTIVE 重复不新增 |
| 关键词 Top-K | `test_memory_store.py` | 相关事实命中；无关事实不注入 |
| user/project 隔离 | `test_memory_store.py`、`test_memory_service.py` | 相同关键词不能跨作用域读取 |
| 失效与删除 | `test_memory_store.py` | 非 ACTIVE 记录不再召回 |
| 防推测/防敏感信息 | `test_memory_policy.py` | 无原文证据、低置信度、凭证候选被拒绝 |
| 类型/来源规则 | `test_memory_policy.py` | 偏好来自用户，流程来自执行证据 |
| 结构化候选提取 | `test_memory_extractor.py` | 强制 extract_memories Tool Call 和 Schema |
| Planner/Executor 注入 | `test_memory_prompting.py`、`test_orchestrator_memory.py` | 两阶段都收到同一带来源上下文 |
| search_memory | `test_memory_service.py` | task_id 注入并派生 scope |
| 记忆管理 API | `test_memory_api.py` | 列出、跨 scope 拒绝、失效、软删除 |
| 失败隔离 | `test_orchestrator_memory.py` | capture 异常后任务仍为 SUCCESS |
| 旧任务兼容 | `test_task_model.py` | 缺 scope 字段时使用本地默认值 |

## 3. 建议命令

```powershell
.venv\Scripts\python.exe -m pytest -q `
  tests/test_memory_store.py `
  tests/test_memory_policy.py `
  tests/test_memory_extractor.py `
  tests/test_memory_service.py `
  tests/test_memory_prompting.py `
  tests/test_orchestrator_memory.py `
  tests/test_memory_api.py `
  tests/test_task_model.py `
  tests/test_task_manager.py `
  tests/test_file_task_store.py `
  tests/test_api.py `
  tests/test_config.py `
  tests/test_security.py
```

聚焦测试通过后执行：

```powershell
.venv\Scripts\python.exe -m pytest -q
```

## 4. 必查行为

1. SQLite 文件在关闭并重新创建 Store 后仍可读取；
2. 同 user/project/type/content 不重复写入；
3. user 或 project 任一不同都无法召回；
4. INVALIDATED/DELETED 不参与初始注入和 search_memory；
5. 记忆上下文包含来源、置信度和时间；
6. LLM 推测、凭证和无原文证据的候选不会落库；
7. capture 失败不改变 SUCCESS 任务；
8. `docs/verification/01_security.md` 不被修改。

## 5. 验收回填（2026-09-28）

- 被测试 Commit SHA：`b9bbe72c57831572364c2ac8bfa7a62229514189`（分支 `main`）
- OS / Python / pytest：Windows 11 25H2（build 26200）/ 3.13.2（`.venv`）/ 9.1.1
- 聚焦结果：`127 passed, 1 skipped, 1 warning in 8.93s`（第 3 节列出的 13 个文件）
- 全量结果：`364 passed, 1 skipped, 1 warning in 11.95s`
- 失败与最小修复：本编号自身无用例失败；验收前置修复两处循环导入
  （`470cb10` 修 `memory/extractor.py`、`b9bbe72` 修 `context/compactor.py`），
  否则第 3 节命令在收集阶段即失败
- `git status --short`：执行测试时为空；随后仅新增本验收记录文件

聚焦命令与输出：

```powershell
.venv\Scripts\python.exe -m pytest -q `
  tests/test_memory_store.py tests/test_memory_policy.py tests/test_memory_extractor.py `
  tests/test_memory_service.py tests/test_memory_prompting.py tests/test_orchestrator_memory.py `
  tests/test_memory_api.py tests/test_task_model.py tests/test_task_manager.py `
  tests/test_file_task_store.py tests/test_api.py tests/test_config.py tests/test_security.py
```

```text
127 passed, 1 skipped, 1 warning in 8.93s
```

跳过项为 `tests/test_security.py:102`（Windows 无法创建符号链接），与本编号无关。

### 5.1 第 4 节必查行为对照

| 必查行为 | 证据 | 判定 |
| --- | --- | --- |
| SQLite 关闭并重建 Store 后仍可读 | `test_memory_store.py` | 通过 |
| 同 user/project/type/content 不重复写入 | `test_memory_store.py` | 通过 |
| user 或 project 任一不同都无法召回 | `test_memory_store.py`、`test_memory_service.py` | 通过 |
| INVALIDATED/DELETED 不参与注入与 `search_memory` | `test_memory_store.py`、`test_memory_service.py` | 通过 |
| 记忆上下文包含来源、置信度与时间 | `test_memory_prompting.py`、`test_orchestrator_memory.py` | 通过 |
| 推测 / 凭证 / 无原文证据的候选不落库 | `test_memory_policy.py` | 通过 |
| capture 失败不改变 SUCCESS 任务 | `test_orchestrator_memory.py` | 通过 |
| `docs/verification/01_security.md` 不被修改 | 本次验收未改动该文件（未出现在 `git status`） | 通过 |

本编号全部为 SQLite + Fake LLM 离线验证，未使用 Docker、MinIO 或真实模型；
按第 6 节收口规则，P1-1 可标记为已完成。

本编号全部是 SQLite/Fake LLM 离线验证，不需要 Docker、MinIO 或真实模型。只有聚焦和
全量回归均通过、实际结果已回填，才把 P1-1 状态改为“已完成”。
