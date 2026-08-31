# Design Note 06：最小 API 层（FastAPI）

## 这一步解决了什么

之前只有命令行脚本，外部无法调用。这一步给出 HTTP 入口：提交任务、查询任务状态/结果。至此 Phase 1 从"用户输入"到"结果落盘"整条链路都有了对外接口。

- `POST /tasks`：创建任务并立即返回 id（后台线程异步执行，不阻塞请求）。
- `GET /tasks/{id}`：轮询任务状态与结果。
- `GET /tasks`：列出全部任务。

## 调用链

```text
POST /tasks
  -> orchestrator.task_manager.create_task(user_input)   -> Task(CREATED)
  -> 后台线程 _run_task -> orchestrator.run_task(task_id)
       -> start -> plan -> execute -> succeed/fail
  -> 立即返回 task.to_dict()（此时多半还是 CREATED/RUNNING）
GET /tasks/{id}
  -> orchestrator.task_manager.get_task(id).to_dict()     -> 404 if missing
```

## 关键代码

- `api/app.py` 的 `create_app(orchestrator=None)` —— 工厂函数，可注入 fake orchestrator 做测试，也便于以后换依赖。
- `api/app.py` 的 `_run_task` —— 后台线程入口，捕获异常（`run_task` 已把任务标 FAILED）。
- `agent/orchestrator.py` 的 `run_task(task_id)` —— 关键拆分：先建任务拿 id、再按 id 执行，是"异步提交 + 轮询"模式的前提。

## 如何实际验证

```powershell
# 起 API（需要 .env 里的 LLM_API_KEY，任务真正执行时才用到）
.venv\Scripts\python.exe -m uvicorn api.app:app --host 127.0.0.1 --port 8000
# 提交任务
curl -X POST http://127.0.0.1:8000/tasks -H "Content-Type: application/json" -d "{\"user_input\":\"...\"}"
# 轮询
curl http://127.0.0.1:8000/tasks/<task_id>
```

离线可验证：`from api.app import app` 能导入构建；`tests/test_api.py` 用 TestClient + fake orchestrator 覆盖提交/轮询/404/列表/参数校验。

## 面试要点

- 为什么"先建任务、后台跑"而不是同步阻塞？→ LLM + 沙箱执行是长任务，同步会占住请求；异步 + 任务状态机让客户端能轮询。
- 为什么 `create_app` 是可注入工厂？→ 测试用 fake、生产用真实组装，边界清晰。
- 任务状态机在这里的价值：`CREATED -> RUNNING -> SUCCESS/FAILED` 让"轮询"有了稳定语义。

## 最容易误解的一点

- POST 返回的 `status` 通常还是 `CREATED`（线程还没跑完），必须轮询 `GET /tasks/{id}` 才拿到最终结果。
- 后台 `threading.Thread` 不是任务队列：进程重启后 in-flight 任务会丢。Phase 2 要换 Celery/Redis 做持久化队列——这正好是"面试能主动说"的演进点。
