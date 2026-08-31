# Design Note 05：具体工具与组装（把 Agent 接进沙箱和存储）

## 这一步解决了什么

上一版的 `agent/` 三件套只依赖一个"工具接口"，本身是空壳——能单测、但连不上真实世界。这一步补上三个具体工具，并加一个组装入口，让整条链路真正能跑。

- `run_python`：把 LLM 生成的代码送进沙箱执行，拿到归一化输出。
- `read_file`：让 Agent 读取沙箱里的文本文件（比如先看一眼 CSV 表头）。
- `save_report`：把沙箱产物刷到 OSS，返回 presigned 下载链接——这是任务"成果"的出口。

## 三个工具与调用链

```text
Executor（tool-calling 循环）
  -> run_python(code)     SandboxTool -> SandboxClient.execute_python
  -> read_file(filename)  FileTool    -> SandboxClient.read_text_file
  -> save_report(f, key)  ReportTool  -> SandboxClient.read_bytes_file
                                      -> StorageManager.upload_file_content -> URL

组装入口：agent/wiring.build_orchestrator()
  Settings -> SandboxClient + StorageManager
          -> ToolRegistry(三个工具)
          -> TaskManager + LLMClient + Planner + Executor
          -> AgentOrchestrator
```

## 关键代码

- `tools/base_tool.py` 的 `BaseTool.to_openai_schema()` —— 每个工具自己声明 JSON Schema，注册后统一生成 Function Calling 定义。
- `tools/report_tool.py` 的 `ReportTool.execute()` —— 二进制读出 + 上传 + 返回 URL，是"结果落盘"的唯一出口。
- `agent/wiring.py` 的 `build_orchestrator()` —— 组合根：把所有依赖在一个地方装配，demo 和之后的 API 都复用。
- `agent/llm_client.py` 的 `_ensure_client()` —— 懒构造 OpenAI 客户端，无 key 时组装/启动不炸，首次 `chat()` 才校验。

## 如何实际验证

真机（最完整，需要真实服务）：

```powershell
# 1. 写 .env：LLM_API_KEY=你的key
# 2. 起服务
docker compose up -d
# 3. 跑端到端
.venv\Scripts\python.exe demo/run_demo.py
```

`run_demo.py` 会走完整闭环：示例 CSV → OSS → pre-hook 进沙箱 → Planner → Executor（LLM 调用 run_python / save_report）→ 结果 URL 回 OSS → 任务 SUCCESS。

离线冒烟（不需要服务）：`build_orchestrator()` 能无网络组装成功。
轻量单测（fake）：`tests/test_concrete_tools.py` 覆盖三个工具的成败映射与数据搬运。

## 面试要点

- 工具是"插件"，Agent 核心（Executor）不认识具体工具，只认 `ToolRegistry` 接口——这是可扩展性的关键，后面加浏览器/MCP 工具都不用改 Executor。
- `save_report` 体现了"执行在沙箱、成果在 OSS"的边界：沙箱无状态、随时可弃，产物必须显式落盘。
- 组合根把装配集中在 `wiring`，业务类（Planner/Executor/Orchestrator）保持纯逻辑、可注入、可测。

## 最容易误解的一点

- Agent 本身不直接"产出文件"：文件在沙箱里生成，必须靠 `save_report` 主动搬到 OSS 才算是任务成果；否则沙箱清洗后什么都没留下。
