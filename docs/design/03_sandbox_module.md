# Design Note 03：沙箱集成层（sandbox/client.py + hooks.py）

## 1. 本次修改解决的工程问题

- **二进制文件崩溃**：原 `hermes_hooks.py` 用 `content.decode('utf-8')` 写文件，xlsx 直接抛异常。改为 base64 二进制写入。
- **输出解析不完整**：原代码只处理 `stream`/`error`，丢掉 `execute_result`/`display_data`，且无输出时返回"执行成功"。`ExecutionResult` 归一化四类输出并保留 `status`。
- **import 副作用**：原 `agent_core.py` 模块级实例化 engine 触网。现在构造只建对象，不发请求。
- **鉴权缺失**：SDK 无 `api_key` 构造参数，统一用 header 注入。

## 2. 完整调用链

```text
pre : HermesHookEngine.pre_execution_hook
        -> StorageManager.download_file_content（字节）
        -> SandboxClient.write_bytes_file（base64）
        -> SDK file.write_file(encoding="base64")

exec: SandboxClient.execute_python
        -> SDK jupyter.execute_code
        -> _normalize -> ExecutionResult（status + 四类输出）

post: HermesHookEngine.post_execution_hook
        -> SandboxClient.read_bytes_file（download_file 字节流）
        -> StorageManager.upload_file_content -> presigned URL
        -> SandboxClient.delete_file（rm -f 安全引用）
```

## 3. 最关键的代码位置及解释

- `sandbox/client.py:28` `ExecutionResult` —— 归一化执行结果，SDK 无关。
- `sandbox/client.py:40` `ExecutionResult.text` —— 把 stdout/结果/错误拼成可回给 LLM 的文本。
- `sandbox/client.py:64` `_build_client` —— API Key 走 `X-AIO-API-Key` header。
- `sandbox/client.py:74` `resolve` —— 相对文件名 -> 沙箱 workspace 绝对路径。
- `sandbox/client.py:85` `write_bytes_file` —— base64 编码后 `encoding="base64"` 写入。
- `sandbox/client.py:99` `read_bytes_file` —— `download_file` 字节流合并。
- `sandbox/client.py:103` `delete_file` —— `shlex.quote` 防注入的 `rm -f`。
- `sandbox/client.py:108` `execute_python` —— 透传 timeout/session_id/cwd。
- `sandbox/client.py:124` `_normalize` —— 四类输出归一化核心。
- `sandbox/hooks.py:19` `HermesHookEngine` —— 注入 sandbox/storage，无 import 副作用。
- `sandbox/hooks.py:30` `pre_execution_hook` —— OSS -> 沙箱。
- `sandbox/hooks.py:37` `post_execution_hook` —— 沙箱 -> OSS + 清洗。

## 4. 必须掌握的知识点

- base64 编码与二进制安全传输。
- Jupyter 输出类型：`stream` / `execute_result` / `display_data` / `error`。
- API Key 通过 header 鉴权（`X-AIO-API-Key`）。
- `shlex.quote` 防 shell 注入。
- 防腐层思想：把 SDK 细节归一化成自己的 `ExecutionResult`，上层不直接依赖 SDK。

## 5. 最容易让我误解的地方

- `write_file` 默认是文本（utf-8），只有 `encoding="base64"` 才是二进制；二进制读出必须用 `download_file`，不是 `read_file`。
- SDK 的 `read_file` 返回 `ResponseFileReadResult`，文本在 `.data.content`，不是 `.content`。
- 沙箱是 Linux，`delete_file` 用 POSIX `rm -f` + `shlex.quote`，不是 Windows 命令。
- 成功判据是 `ExecutionResult.status`，"没有输出"不等于成功。

## 6. 我应该主动回答的 3~5 个问题

- 二进制怎么进沙箱？→ base64 + `encoding="base64"`（SDK 原生能力）。
- 怎么判断代码执行成功？→ 看 `status` + 归一化四类输出，不被"无输出"骗。
- API Key 怎么传？→ header 注入，SDK 没有构造参数。
- 为什么包一层 `SandboxClient`？→ 防腐层，把 SDK 返回归一化，工具/executor 不依赖 SDK 细节。

## 7. 如何测试本模块

```powershell
.venv\Scripts\python.exe -m pytest tests/test_sandbox_client.py tests/test_sandbox_hooks.py -q
```

用 fake SDK：路径、base64、字节流、`rm` 引用、四类输出、timeout、header、OSS↔沙箱搬运与清洗。

## 8. 面试官最可能追问的 5 个问题

- base64 让文件变大 33%，值得吗？→ 换取通用性与简单性；超大文件可改 `upload_file` 流式。
- 执行超时怎么办？→ `status="timeout"` 由 `ExecutionResult` 保留，executor 决定重试/失败。
- 沙箱执行任意代码安全吗？→ 沙箱隔离 + Phase 2 权限层做命令/路径白名单。
- 文本和二进制为什么走不同 API？→ `read_file` 只回文本，二进制必须 `download_file`。
- `str_replace_editor` 是什么？→ SDK 内置文档查看器，能按 sheet/page/slide 读 Excel/PDF/PPTX。

## 9. 项目核心代码（必须真正理解）

- `write_bytes_file` / `read_bytes_file`（二进制安全）。
- `_normalize`（四类输出归一化）。
- `HermesHookEngine` 的 pre/post（数据搬运闭环）。

## 10. 框架/基础设施细节（暂可不深入）

- agent-sandbox 内部 Fern 生成的 client_wrapper / http 细节。
- `str_replace_editor` 的全部参数（Phase 2 用到再深入）。
