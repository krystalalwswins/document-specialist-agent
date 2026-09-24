# P1-1 本地长期记忆验收说明

> 记录日期：2026-09-23  
> 实现分支：`feat/harness-p1-1`  
> 开发基线：`feat/harness-p0-5` / `d5be978`（远端）  
> 当前状态：实现与测试代码完成，未执行测试；以下内容交由独立 Codex 验收。

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

## 5. 验收后回填

- 被测试 Commit SHA：`PENDING`
- OS / Python / pytest：`PENDING`
- 聚焦结果：`PENDING`
- 全量结果：`PENDING`
- 失败与最小修复：`PENDING`
- `git status --short`：`PENDING`

本编号全部是 SQLite/Fake LLM 离线验证，不需要 Docker、MinIO 或真实模型。只有聚焦和
全量回归均通过、实际结果已回填，才把 P1-1 状态改为“已完成”。
