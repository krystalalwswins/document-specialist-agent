# Design Note 10：Retry 收尾与 Security 基础版

## 1. 本次修改解决的工程问题

PermissionError/FileNotFoundError 原先落入 OSError 重试分支；Python 超时可能重复产生副作用；工具参数和工作区范围没有统一校验。现在将“错误可重试”和“工具可安全重放”分开判断，Registry 在执行前校验参数与权限，SandboxClient 对文件路径再次检查。

SDK 0.0.30 返回 execution envelope 的 `.data`。本轮基线实测 75 passed / 4 failed，修正归一化后原 79 个测试通过；新增边界测试后结果见 [验证记录](../verification/01_security.md)。

## 2. 完整调用链

1. 我们的 Executor 收到模型调用，创建 TaskStep，再解析 JSON。
2. 我们的 Registry 校验已注册工具、本地 JSON Schema、PermissionManager 策略。
3. 我们的具体工具调用 SandboxClient 或 StorageManager；FileTool 声明 retry_safe，执行代码和上传默认不声明。
4. 我们的 SandboxClient 校验路径，调用 SDK 在远程 Linux 中执行固定路径检查程序，再进行文件 IO。
5. Python 调用使用 SDK 创建独立 session、传入受限 timeout，finally 删除该 session。
6. Executor 逐次写入 retry_events；拒绝额外写 security_events；普通失败反馈模型，执行状态不确定则抛出终止异常，由 Orchestrator 标记任务 FAILED。

## 3. 最关键的代码位置及解释

| 位置 | 要理解的内容 |
| --- | --- |
| `retry/retry_policy.py::classify_exception` | 具体异常先于基类，保留 StorageError 的显式异常链 |
| `retry/retry_policy.py::RetryPolicy.decide` | 纯决策，两个重试条件必须同时满足 |
| `tools/base_tool.py::BaseTool` | retry_safe 默认 False；权限和文件参数由工具声明 |
| `tools/tool_registry.py::execute` | Schema 和权限检查在真实工具前；拒绝不产生 IO |
| `security/permission_manager.py::workspace_path` | POSIX 路径分段判断，不能用 startswith 当目录边界 |
| `sandbox/client.py::PATH_GUARD` | 在远程沙箱检查现有符号链接，不能在宿主机 resolve 远程路径 |
| `sandbox/client.py::execute_python` | 默认/上限超时、关闭 SDK 重试、独立 session 和清理 |
| `agent/executor.py::_invoke_tool` | 逐次事件、关联 ID、安全拒绝审计 |
| `agent/executor.py::run` | 非法参数反馈及 terminal 终止分支 |
| `docker-compose.yaml` | CPU、总内存、swap、PID 配额；与 shm_size 分开 |

## 4. 必须掌握的知识点

- 幂等性：服务端执行成功、响应丢失时，客户端不能仅凭超时断言没有执行。
- 纵深防御：工具权限、远程路径检查和容器约束解决不同问题。
- Function Calling 协议：普通失败仍需要对应 tool_call_id 的 tool 消息。
- Python 异常继承：PermissionError、FileNotFoundError 属于 OSError。
- finally：成功、报错、超时都应尝试清理自己的 session。

## 5. 最容易误解的地方

- 新 session 隔离 Python 变量，不隔离同一容器的文件和网络。跨轮变量不再保留，需通过文件传递。
- 检查路径的符号链接后再读取仍存在 TOCTOU；恶意并发代码可改变路径。本原型不是恶意多租户服务。
- run_python 具有容器用户权限，能绕过 read_file 的路径规则；白名单可关闭该工具，不能用 AST/字符串黑名单宣称完全限制 Python。
- 超时后删除 session 是清理请求；本批不承诺杀死所有派生进程。真实烟测只验证一个延迟写文件场景。
- security_events 不记录参数、代码或签名 URL；现有步骤输出/错误仍需后续统一脱敏和持久化。
- 默认共享工作区、全局 reports 前缀、内存事件仍存在；任务级命名空间和轨迹持久化按路线图后续实施。

## 6. 我应该主动回答的问题

**为什么把权限放在 Registry？** 它是所有模型工具调用的公共分发入口，新增工具声明元数据后可复用检查。

**为什么默认不能重试？** 通用工具的副作用未知，显式 opt-in 比默认重复执行更可靠。

**为什么不把所有错误都判任务失败？** 参数、权限和确定性代码错误可让模型换方案；执行状态不确定则停止，避免重复副作用。

**新增工具需要改 Executor 吗？** 不需要。注册 Schema、声明权限/路径参数和重试安全性即可；生产白名单必须显式允许新名称。

## 7. 如何测试本模块

```bash
python -m pip install -r requirements.txt
python -m pytest -q
python -m demo.retry_demo
python -m demo.security_demo
```

以上不调用真实 LLM/MinIO/Docker；符号链接测试在临时本地目录执行与远程相同的固定检查程序。SDK 响应模型测试使用真正安装的 0.0.30 类型，其他 SDK 行为用 fake。

```bash
docker compose up -d
python -m demo.security_smoke
```

烟测需要 Docker、镜像和 `.env` 的沙箱设置，不需要 LLM key。退出 0 表示所列检查通过；退出 2 表示没有验证，不算通过；断言失败为失败。运行前使用本项目沙箱与工作区，不要指向其他业务服务。

## 8. 面试官最可能追问的 5 个问题

1. **超时等于代码停止吗？** 不等于；分别限制 SDK 请求和执行时间，清理会话并实测，不能对所有子进程作保证。
2. **文件白名单能约束 Python 吗？** 不能，任意 Python 的可信边界是容器；本项目不承诺恶意租户隔离。
3. **多个任务会抢同一个文件吗？** 当前共享目录仍可能，任务目录和对象命名空间属于批次 3 验收。
4. **事件重启后还能查吗？** 当前不能，已改为逐次记录，但持久化属于批次 2。
5. **预签名 URL 是用户权限系统吗？** 不是，是有效期内持有即可访问的凭证；当前实现也没有账号鉴权。

## 9. 项目核心代码（必须真正理解）

重试判据：`error_type.retryable and registry.retry_safe(name)`，终止性结果优先于重试。调用边界：`Schema → Permission → Tool`；文件工具边界：`规范化 → 远程检查 → IO`。检查失败则真实操作不执行。

## 10. 框架/基础设施细节

JSON Schema 使用 jsonschema，限制外部引用以避免校验触网。SDK 使用 request_options 关闭底层重试；boto3 同样配置单次尝试，避免应用层之外的隐式重放。容器配额参考 Docker 官方文档，实际生效仍需真实环境证据。
