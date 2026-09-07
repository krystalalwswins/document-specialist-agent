# Security Design Review

## 1. 问题与目标

基线 commit `40c16a8`：PermissionError 被 OSError 分支归为瞬态；任意工具可重复执行；路径用 startswith 检查；模型可指定无上限 timeout；没有 CPU/总内存/PID 配额；工具 JSON 解析失败会退出循环。

本批建立可验证的应用层边界，保留原有三层架构。不会用 Python 黑名单声称实现恶意代码隔离。

## 2. 调用链与策略

1. Executor 创建步骤，解析 JSON。非法参数返回 INVALID_ARGUMENT，保持 tool_call_id 配对。
2. Registry 校验本地工具 Schema（禁止远程 $ref），PermissionManager 检查工具白名单、工具声明的文件参数和对象 key 参数。拒绝返回 PERMISSION_DENIED，不调用工具。
3. SandboxClient 对所有文件进出再次规范化 POSIX 路径；在沙箱侧检查路径及祖先符号链接，检查失败时拒绝操作。应用层检查与实际 IO 存在 TOCTOU 窗口，不能代替内核边界。
4. 工具通过 retry_safe 显式声明重放安全，默认 False。只有错误可重试且工具安全才进入退避。read_file 可重试；run_python/save_report 默认不可自动重试。
5. 每次拒绝和尝试关联 task/step/call ID，不记录参数、代码或 URL 签名到安全审计事件。

## 3. Python、会话和超时

- 使用 SDK 0.0.30，核对安装包与上游源码的响应 envelope、request_options、会话创建/删除接口。
- 默认执行 30 秒，平台上限 120 秒；模型参数只能在范围内。HTTP 等待有额外有限宽限，SDK 自动重试关闭，防止绕过 Executor 的重放约束。
- 每次 Python 调用使用独立 Jupyter session，并在结束后请求删除；跨调用数据通过文件传递。此选择避免共享变量污染，但会改变依赖跨轮变量的任务；共享文件系统仍然存在。
- timeout、网络异常等不能确定执行状态的情况终止当前任务，不交给模型继续重放。删除 session 是清理请求，不能证明所有派生进程已停止；真实容器验证前保持 IN_PROGRESS。
- 可通过工具白名单禁用 run_python；开放时假定是单用户开发沙箱，不声称文件白名单可以约束任意 Python 的全部行为。

## 4. 容器配置

增加可配置 cpus、mem_limit、memswap_limit、pids_limit；MinIO 端口绑定 loopback。保留上游镜像的 seccomp 配置并明确限制，不盲目收紧到无法启动。无宿主目录/Docker socket 挂载。配额需 docker inspect 与容器内受控检查验证。

参考：[Compose services](https://docs.docker.com/reference/compose-file/services/)、[Docker resource constraints](https://docs.docker.com/engine/containers/resource_constraints/)、[上游 Jupyter SDK](https://github.com/agent-infra/sandbox/blob/main/sdk/python/agent_sandbox/jupyter/client.py)。实现以本地安装的 0.0.30 为准，不使用未核实的 interrupt API。

## 5. 验证矩阵

| 场景 | 预期 |
| --- | --- |
| 权限异常、文件不存在 | 分类准确，不重试 |
| 安全读瞬态失败 | 有界恢复，每次尝试可追踪 |
| Python/上传超时 | 不自动重放；Python 执行状态不确定则任务失败 |
| 非法 JSON、类型或多余参数 | 底层零执行，有结构化失败反馈 |
| 未授权工具、路径穿越、相似前缀、符号链接、越界对象 key | 拒绝并审计，无目标 IO |
| 正常文件/报告/Python | 保持主链路可用 |
| 默认/过大/负数 timeout | 默认生效，越界参数不执行 |
| 会话清理失败 | 不伪装成功，任务不能继续执行 |
| Docker 配额、超时代码清理 | 真实环境脚本验证，不用 fake 代替 |

## 6. 批次边界

本批事件仍在内存，逐次写入与关联为后续持久化铺路；任务目录与对象命名空间细分在批次 3；权限控制不等于账号认证；代码运行网络访问控制、恶意进程逃逸与多租户隔离不在本原型承诺内。
