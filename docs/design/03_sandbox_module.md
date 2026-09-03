# Design Note 03：沙箱集成层（sandbox/client.py + hooks.py）

## 模块作用

对 `agent-sandbox` SDK 做一层薄封装：`SandboxClient` 提供统一的执行/文件读写（含二进制），`HermesHookEngine` 负责 OSS 与沙箱之间的数据搬运与产物清洗。

## 设计原因（解决什么 Agent 工程问题）

1. **原代码的二进制 bug**：`hermes_hooks.py` 用 `content.decode('utf-8')` 写文件，xlsx 直接抛异常。本模块改为统一的 base64 二进制写入。
2. **输出解析不完整**：原代码只处理 `stream` 和 `error`，丢掉 `execute_result`/`display_data`；且无输出时返回"执行成功"会掩盖真实失败。`ExecutionResult` 统一归一化四类输出，并保留 `status`（ok/error/timeout）。
3. **import 副作用**：原 `agent_core.py` 模块级实例化 engine 触网。现在 `SandboxClient`/`HermesHookEngine` 构造只建对象，不发请求。
4. **鉴权**：SDK 无 `api_key` 构造参数，API Key 必须走 `headers`，统一在 `_build_client` 处理。

## 核心流程

```text
pre_execution_hook(oss_key, filename)
    OSS download_file_content -> bytes
    -> SandboxClient.write_bytes_file  (base64)

post_execution_hook(filename, oss_key)
    SandboxClient.read_bytes_file  (download_file 字节流)
    -> StorageManager.upload_file_content -> presigned URL
    -> SandboxClient.delete_file  (rm -f 安全引用)

execute_python(code) -> jupyter.execute_code -> ExecutionResult
    stream/execute_result/display_data/error 统一归一化
```

## 关键代码

二进制写入（`sandbox/client.py`）：

```python
def write_bytes_file(self, filename: str, content: bytes) -> str:
    path = self.resolve(filename)
    encoded = base64.b64encode(content).decode("ascii")
    self._client.file.write_file(file=path, content=encoded, encoding="base64")
    return path
```

输出归一化：`stream` -> stdout/stderr，`error` -> error+traceback，`execute_result`/`display_data` -> `text/plain`。

## 面试回答

**"怎么把 Excel 这样的二进制文件安全送进沙箱？"**
先读 SDK 源码确认 `write_file` 的 `encoding` 支持 `utf-8/base64/raw`。二进制统一 base64 编码写入、用 `download_file` 字节流读出，不再假设文本，彻底解决 xlsx 场景。

**"怎么判断沙箱代码执行成功？"**
不只看有没有 print，而是归一化 `status`（ok/error/timeout）和四类 output；`ExecutionResult.text` 把 stdout/结果/错误拼成可供 LLM 回看的文本，错误不会被"执行成功（无输出）"掩盖。

**"API Key 怎么传？"**
SDK 没有 `api_key` 参数，README 明确用 `X-AIO-API-Key` header；`_build_client` 里只在配置了 key 时才注入 header，保持本地免密可用。

## 如何测试

`tests/test_sandbox_client.py`：路径解析、文本/base64 写入、字节流读取、`rm -f` 安全引用、四类输出归一化、timeout 透传、API Key header。
`tests/test_sandbox_hooks.py`：OSS->沙箱 与 沙箱->OSS 的搬运、产物清洗、执行文本返回。

全部用 fake SDK，无需 Docker。运行：`python -m pytest tests/ -q`

Agent 的完整执行链路：

LLM 生成 Python 代码
    │
    ▼
HermesHookEngine.pre_execution_hook()  ← 把数据从 OSS 搬到沙箱
    │
    ▼
SandboxClient.execute_python()         ← clients中方法，执行代码
    │
    ▼
ExecutionResult.text                   ← 执行结果喂回 LLM
    │
    ▼
HermesHookEngine.post_execution_hook() ← 把结果从沙箱搬回 OSS
    │
    ▼
LLM 决策下一步