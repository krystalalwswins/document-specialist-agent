"""StorageManager tests with a fake S3 client (no network, no MinIO)."""

from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from core.config import Settings
from storage.storage_manager import StorageError, StorageManager


class FakeS3Client:
    def __init__(self, existing_bucket=False):
        self.buckets = {"doc-agent-storage"} if existing_bucket else set()
        self.objects = {}
        self.head_bucket_calls = 0
        self.create_bucket_calls = 0
        self.put_object_calls = 0

    def head_bucket(self, Bucket):
        self.head_bucket_calls += 1
        if Bucket not in self.buckets:
            raise ClientError({"Error": {"Code": "NoSuchBucket"}}, "HeadBucket")

    def create_bucket(self, Bucket):
        self.create_bucket_calls += 1
        self.buckets.add(Bucket)

    def put_object(self, Bucket, Key, Body):
        self.put_object_calls += 1
        self.objects[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):
        key = (Bucket, Key)
        if key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        body = self.objects[key]
        return {"Body": SimpleNamespace(read=lambda: body)}

    def generate_presigned_url(self, *args, **kwargs):
        key = kwargs["Params"]["Key"]
        return f"https://presigned.example/{key}"


def _build(existing_bucket=False):
    fake = FakeS3Client(existing_bucket=existing_bucket)
    manager = StorageManager(settings=Settings(_env_file=None), s3_client=fake)
    return manager, fake


def test_construction_has_no_side_effects():
    _, fake = _build()
    assert fake.head_bucket_calls == 0
    assert fake.create_bucket_calls == 0


def test_upload_creates_bucket_lazily_and_returns_url():
    manager, fake = _build()
    url = manager.upload_file_content("a/b.txt", b"hello")
    assert fake.head_bucket_calls == 1
    assert fake.create_bucket_calls == 1
    assert "doc-agent-storage" in fake.buckets
    assert fake.objects[("doc-agent-storage", "a/b.txt")] == b"hello"
    assert url == "https://presigned.example/a/b.txt"


def test_bucket_created_only_once():
    manager, fake = _build()
    manager.upload_file_content("a.txt", b"1")
    manager.upload_file_content("b.txt", b"2")
    assert fake.create_bucket_calls == 1


def test_existing_bucket_not_recreated():
    manager, fake = _build(existing_bucket=True)
    manager.upload_file_content("a.txt", b"1")
    assert fake.head_bucket_calls == 1
    assert fake.create_bucket_calls == 0


def test_binary_round_trip():
    manager, _ = _build()
    payload = b"\x00\x01\xfe\xff"
    manager.upload_file_content("x.bin", payload)
    assert manager.download_file_content("x.bin") == payload


def test_download_missing_raises_storage_error():
    manager, _ = _build()
    with pytest.raises(StorageError, match="download failed"):
        manager.download_file_content("missing.bin")


def test_head_bucket_other_error_is_wrapped():
    class BrokenS3(FakeS3Client):
        def head_bucket(self, Bucket):
            self.head_bucket_calls += 1
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "HeadBucket")

    manager = StorageManager(settings=Settings(_env_file=None), s3_client=BrokenS3())
    with pytest.raises(StorageError, match="cannot reach bucket"):
        manager.upload_file_content("a.txt", b"1")


def test_presign_failure_is_wrapped():
    class BrokenS3(FakeS3Client):
        def generate_presigned_url(self, *args, **kwargs):
            raise ClientError({"Error": {"Code": "X"}}, "GetObject")

    manager = StorageManager(settings=Settings(_env_file=None), s3_client=BrokenS3())
    with pytest.raises(StorageError, match="presign failed"):
        manager.generate_presigned_url("a.txt")
