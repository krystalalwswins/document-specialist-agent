# 学习版验证记录

## 已核实的第一轮完整 CI

- 代码提交：`60ba863fdb570ddb8c06ecfb8988969596120714`。
- GitHub Actions：[运行 34183870960](https://github.com/krystalalwswins/document-specialist-agent/actions/runs/34183870960)。
- 环境：Ubuntu 24.04、Python 3.12.14；安装 requirements-mcp.txt 成功。
- `python -m pytest -q`：**159 passed，1 warning，9 subtests passed**。
- `python -m demo.learning_demo`：通过，生成已验证 CSV 合计 60。
- `python -m evaluation.run`：3/3 固定场景通过，包含 CSV 正数、CSV 负数与零、XLSX。

本地环境仅运行了标准库核心测试与文件处理演示。全量依赖安装因网络审批取消，改用上述正常 GitHub CI 验证，未将历史 135 项测试结果当作本次结果。

## 后续增量

增加了轨迹深拷贝修正、token 记录测试和真实 MCP HTTP 协议回环测试。本地 `python -m unittest discover -s tests -p test_learning_runtime.py -q`：24 项通过。

最终代码提交：`98e8895039c429248dd69ac8ae947d148ab9d206`。

- [最终完整 CI：34184363319](https://github.com/krystalalwswins/document-specialist-agent/actions/runs/34184363319)，结论 SUCCESS。
- `python -m pytest -q`：**162 passed，3 warnings，9 subtests passed，4.30 秒**。
- `tests/test_mcp_parser.py::test_real_mcp_http_parser_roundtrip` 实际启动独立本机 MCP 服务，完成 initialize/list_tools/call_tool 与 CSV 解析，并验证不存在的工具被拒绝。不是协议替身。
- 离线学习 demo 和 3/3 固定评估场景再次通过。
- 3 条警告来自依赖弃用提示；无失败或跳过。此后提交仅追加本文验证记录，运行代码与受测提交相同。

## 验证边界

- SQLite 是真实磁盘数据库，覆盖独立进程重读、多个管理器并发领取和异常恢复。
- CSV/XLSX 是实际字节文件，校验真实表头、行数和固定合计值。
- 离线 workflow 的 LLM、沙箱文件后端、对象存储和 dispatcher 是显式测试替身，不执行生成代码；生产 Registry 另由既有测试验证。
- 真实 LLM 调用、Docker + MinIO 联合验收本轮未运行；可用 demo.run_demo / demo.artifact_smoke 在桌面环境验证。
- SEC-001 未修复，旧 security_smoke 失败证据继续有效。未放宽断言，未将失败转为跳过。

## 桌面端可执行命令

不要求现在修复超时问题。学习可以立即开始；有需要时在项目虚拟环境执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-mcp.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m demo.learning_demo
.\.venv\Scripts\python.exe -m evaluation.run
.\.venv\Scripts\python.exe -m demo.artifact_smoke
# 下面一项会真实调用模型
.\.venv\Scripts\python.exe -m demo.run_demo
```

返回 commit SHA、测试摘要、artifact_smoke 输出和真实模型产物内容即可。不需要发送 .env、密钥或完整原始文档。
