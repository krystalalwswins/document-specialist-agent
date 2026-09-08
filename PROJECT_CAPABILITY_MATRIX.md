# 当前能力矩阵

按“可学习的完整原型”组织：核心功能已经落地，外部真实验收与已知缺陷单独列出。以 [验证记录](docs/verification/02_learning_runtime.md) 为证据，不沿用早期阶段的 DONE 标签。

| 简历承诺 / 增强 | 当前实现 | 关键入口 | 边界 |
| --- | --- | --- | --- |
| 三层 Agent Loop | 规划、Tool Calling、结果反馈与最终检查 | agent/orchestrator.py、planner.py、executor.py | 模型真实质量需真实评估 |
| 终止控制 | 最大轮数、工具尝试预算、协作式任务截止时间 | agent/runtime.py | 截止检查不等于终止正在阻塞的代码 |
| 任务/步骤状态 | 两级状态机、时间、错误、终态收尾 | task/task_model.py、task_manager.py | 不重新启动终态任务 |
| 完整可见轨迹 | 计划、模型可见请求/回复、调用关联、每次尝试、结果落盘 | task/sqlite_store.py、agent/executor.py | 不保存隐藏思考；本机数据库包含任务内容 |
| 异步与恢复 | SQLite 待执行任务、原子领取、固定 worker、显式中断恢复 | task/worker.py、recover.py | 不自动重放不确定执行；本机小规模队列 |
| 可插拔工具 | 注册、Schema、权限、分发与统一 ToolResult | tools/tool_registry.py | 需在 wiring 和配置中启用 |
| Docker 安全执行 | 配额、路径检查、独立会话、超时配置 | sandbox/client.py、docker-compose.yml | SEC-001 未修复；非恶意多租户隔离 |
| 输入与文档 | Base64 输入、任务目录、CSV/XLSX/TXT/MD/JSON 解析 | documents/files.py | 2 MiB/5 文件；表格行列限制 |
| 产物闭环 | 非空、格式、列/行要求、SHA-256、对象 key、签名 URL | tools/report_tool.py | 结构校验不证明所有业务计算；真实 S3 烟测另验 |
| 异常恢复 | 错误分类、安全重试、退避抖动、有限产物纠正 | retry/retry_policy.py、agent/executor.py | 不保证任意 Python exactly-once |
| Memory | 完整消息组裁剪、大输出引用、显式脱敏笔记检索 | memory、tools/history_tool.py | 字符近似预算；关键词检索；正则脱敏有限 |
| Evaluation | usage/耗时/成功率/产物率/重试率、固定 CSV/XLSX 集 | evaluation | 固定替身评估不代表真实 LLM 成功率 |
| MCP | 可选 HTTP 解析适配器及本地标准协议服务 | tools/mcp_document_tool.py、demo/mcp_parser_server.py | 配置的可信服务；SDK v1；协议验证单列 |

学习所需链路已覆盖全部模块。最终“实现并验证的能力 ≥ 简历承诺”仍以真实集成验收和 SEC-001 关闭为条件；本轮不把延期等同于通过。
