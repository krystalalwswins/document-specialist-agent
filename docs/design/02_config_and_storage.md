# Design Note 02：配置与对象存储（core/config.py + storage/）

## 1. 本次修改解决的工程问题

- **配置散落、`.env` 不生效**：原 `config.py` 是模块级常量，`python-dotenv` 声明了却没用。改为 pydantic-settings + `get_settings()` 单例。
- **import 即触网**：原 `StorageManager.__init__` 直接 `create_bucket`。改为懒创建，构造只建 client。
- **boto3 异常外泄**：统一包装成 `StorageError`，调用方能区分根因。
- **测试依赖真实 MinIO**：`settings`/`s3_client` 可注入，单测用 fake。

## 2. 完整调用链

```text
业务代码 -> get_settings()（lru_cache）-> Settings（读 .env / 环境变量）
业务代码 -> StorageManager(settings?, s3_client?)
  -> upload_file_content -> ensure_bucket（head_bucket；仅 404 才 create）-> put_object -> presigned URL
  -> download_file_content -> get_object -> Body.read()
```

## 3. 最关键的代码位置及解释

- `core/config.py:15` `Settings` —— 字段 + `model_config`（`env_file`、`extra=ignore`）。
- `core/config.py:40` `get_settings` —— `lru_cache` 单例，避免重复读 `.env`。
- `storage/storage_manager.py:24` `StorageManager.__init__` —— 注入 `settings`/`s3_client`，`_bucket_ready=False`。
- `storage/storage_manager.py:36` `_build_client` —— 从配置构建 boto3 client。
- `storage/storage_manager.py:45` `ensure_bucket` —— 懒创建 + 404 白名单 + `RLock` 幂等。
- `storage/storage_manager.py:66` `upload_file_content` —— 先确保 bucket 再写，返回 presigned URL。
- `storage/storage_manager.py:77` `download_file_content` —— 读字节。
- `storage/storage_manager.py:85` `generate_presigned_url` —— 临时安全下载链接。

## 4. 必须掌握的知识点

- pydantic-settings：`BaseSettings`、环境变量自动映射（`SANDBOX_BASE_URL` -> `sandbox_base_url`）。
- `functools.lru_cache` 做单例。
- 依赖注入：构造传依赖，而非在类里 `new`。
- 懒加载 + 幂等：`ensure_bucket` 首次写入才建、只建一次。
- boto3 S3 client 与 `ClientError.response["Error"]["Code"]`。
- presigned URL 的作用与时效（`ExpiresIn`）。

## 5. 最容易让我误解的地方

- `Settings(_env_file=None)` 只是测试里关闭 `.env` 读取；正常 `get_settings()` 会读 `.env`。
- 构造 `StorageManager` 不会建 bucket，首次写入才建——"import 无副作用"是刻意设计。
- `head_bucket` 404 的 code 不一定是 `"404"`，可能是 `"NoSuchBucket"`/`"NotFound"`，所以用白名单判断。
- 环境变量名与字段名大小写无关，`MINIO_BUCKET_NAME` 自动映射到 `minio_bucket_name`。

## 6. 我应该主动回答的 3~5 个问题

- 为什么 bucket 懒创建？→ 构造不触网，保证可测、可并发。
- 为什么注入 `s3_client`？→ fake 单测 + 未来换 OSS/S3 实现。
- 为什么错误要包装？→ 调用方能区分网络/权限/bucket，而不是裸 boto3 异常。
- `get_settings` 为什么缓存？→ 避免每处重读 `.env`、重复构建对象。

## 7. 如何测试本模块

```powershell
.venv\Scripts\python.exe -m pytest tests/test_config.py tests/test_storage_manager.py -q
```

用 fake S3 client 验证：懒创建、幂等、404 与其他错误分支、二进制往返、异常封装。

## 8. 面试官最可能追问的 5 个问题

- `head_bucket` 返回 403 会怎样？→ 不在 404 白名单，抛 `StorageError`，不误建 bucket。
- 并发首次上传会建两个 bucket 吗？→ `RLock + _bucket_ready` 标志，幂等。
- 怎么换阿里云 OSS？→ OSS 兼容 S3，改 endpoint/凭据即可；不兼容则换实现。
- presigned URL 泄露风险？→ 有 `ExpiresIn` 时效，且内容只读。
- 为什么 region 写死 `us-east-1`？→ S3 兼容实现要求，MinIO 忽略 region。

## 9. 项目核心代码（必须真正理解）

- `ensure_bucket` 的懒加载 + 404 白名单 + 幂等。
- `StorageManager` 注入式构造。
- `get_settings` 的 `lru_cache` 单例。

## 10. 框架/基础设施细节（暂可不深入）

- boto3 内部签名与重试机制。
- pydantic-settings 的高级校验能力（validator、secrets 等）。
