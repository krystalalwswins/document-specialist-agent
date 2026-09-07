# 第一批验证记录

日期：2026-09-07。基线：`40c16a8b0df4787b408fb923143dd5cb4c129d6a`。环境：Linux、Python 3.12、agent-sandbox 0.0.30；未使用 LLM API key。

## 实际结果

| 检查 | 结果 | 范围 |
| --- | --- | --- |
| 修改前 `python -m pytest -q` | 75 passed, 4 failed | 四处沙箱结果 envelope 解析错误；不沿用历史 79 全绿结论 |
| 修改后原有测试 | 79 passed | 原模块回归；重试 fake 显式声明安全重放 |
| 新增安全测试后的全量测试 | 130 passed | 51 个新增参数化/集成边界用例，含本地真实符号链接、SDK 类型契约 |
| `python -m demo.security_demo` | 6 场景 PASS | 正常调用、未授权工具、越界路径、非法 JSON、真实 PermissionError、禁止不安全重放 |
| `python -m demo.retry_demo` | 4 场景符合预期 | 2 次恢复、3 次耗尽、参数/权限各 1 次 |
| `python -m demo.security_smoke` | 退出 2：NOT VERIFIED | 当前没有 docker 命令，真实沙箱检查未执行 |

本轮安装版本：pytest 9.1.1、jsonschema 4.26.0、boto3 1.43.89、openai 3.8.0、FastAPI 0.141.1。

测试环境有一条 Starlette/AnyIO 弃用警告，不影响测试通过。依赖安装于独立虚拟环境；pytest 的 SDK/S3/LLM 为 fake，不能据此声称真实容器和对象存储链路通过。

## 仍需真实环境验收

运行 `docker compose up -d` 后执行 `python -m demo.security_smoke`。该脚本核查运行中的配额、文件进出、独立会话、超时后延迟副作用是否出现。保存标准输出、镜像版本和 SDK 版本。

- 容器 CPU/内存/PID 配额已配置，但尚无运行时生效证据。
- 超时参数传递、会话清理、终止任务已离线验证；真实代码停止尚未验证。脚本延迟写文件探针也不覆盖恶意派生进程。
- 真实 CSV/XLSX + LLM + MinIO 端到端、URL 到期、产物校验在批次 3 补齐。
- 任务轨迹仍在内存，任务隔离目录/对象前缀、多租户安全未完成。

第一批状态：**代码和离线验证完成；真实沙箱验收待完成（IN_PROGRESS）**。最终“实现并验证 ≥ 简历承诺”的总标准尚未全部达成，后续批次见 [交付计划](../DELIVERY_PLAN.md)。


## 桌面真实验收补充（用户提供）

对提交 `a514667a092f54b1b83d200461b91fed2c597698` 的验收：真实 Docker 烟测退出 1，超时后发生延迟写文件；配额、读写、越界拒绝、独立会话通过。Windows 离线测试 129 passed、1 failed（符号链接创建权限不足）。此结果更新此前“真实验证待执行”的状态：安全验收确定未通过，仍为 IN_PROGRESS。详细环境、证据与用户批准的延期安排见 [KNOWN_ISSUES.md](../KNOWN_ISSUES.md)。此前 Linux 离线测试通过的记录不构成真实沙箱超时能力的证明。
