# Design Note 02：配置与对象存储（core/config.py + storage/）

## 模块作用

集中、类型化的配置层（pydantic-settings + `.env`），以及一个无 import 副作用、可注入、错误可解释的对象存储封装。

## 设计原因（解决什么 Agent 工程问题）

1. 原 `config.py` 是散落的模块级常量，`python-dotenv` 声明了却从未使用，`.env` 不生效。
2. 原 `StorageManager.__init__` 直接 `create_bucket`，任何 `import` 都触发网络，导致无法单测、无法并发、import 即炸。
3. boto3 异常直接外泄，调用方无从判断是网络、权限还是 bucket 不存在。
4. 硬编码 endpoint/凭据，测试必须依赖真实 MinIO。

## 核心流程

```text
Settings  <-  环境变量 / .env（SANDBOX_BASE_URL 自动映射为 sandbox_base_url）
   `-> get_settings()  （lru_cache 单例）

StorageManager(settings=None, s3_client=None)   # 构造只建 client，不触网
   `-> upload_file_content()
        `-> ensure_bucket()   # head_bucket；仅 404 才 create，幂等 + 线程锁
             `-> put_object / get_object / generate_presigned_url
                  `-> ClientError 统一包装为 StorageError
```

## 关键代码

缓存单例（`core/config.py`）：

```python
@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
```

懒创建与 404 判定（`storage/storage_manager.py`）：

```python
except ClientError as exc:
    code = exc.response.get("Error", {}).get("Code", "")
    if code not in ("404", "NoSuchBucket", "NotFound"):
        raise StorageError(f"cannot reach bucket '{self.bucket}': {exc}")
    self._s3_client.create_bucket(Bucket=self.bucket)
```

## 面试回答

**"为什么用 pydantic-settings？"**
配置集中、有类型和默认值、环境变量自动映射，`get_settings()` 用 `lru_cache` 返回单例，避免每处重复读 `.env`；后续加字段只需改一个类。

**"为什么 bucket 懒创建、构造不触网？"**
构造器不该有网络副作用。`import` 无副作用是可测性和可并发的底线：单测可以注入 fake，多线程共享一个实例时用锁保证 `ensure_bucket` 幂等。

**"为什么可注入 s3_client？"**
依赖注入。测试用 fake 验证行为而非依赖真实 MinIO；未来换 OSS/S3 实现时，只要符合 boto3 client 的接口约定即可替换。

## 如何测试

`tests/test_config.py`：默认值、环境变量覆盖、单例缓存。
`tests/test_storage_manager.py`：用 fake S3 client 验证懒创建、幂等、404 与其他错误分支、二进制往返、异常封装。

运行：`python -m pytest tests/ -q`
