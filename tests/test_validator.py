"""ArtifactValidator: a task must not claim success with a broken deliverable."""

from agent.validator import ArtifactValidator
from storage.storage_manager import StorageError


class FakeStorage:
    def __init__(self, stats=None, errors=None):
        self.stats = dict(stats or {})
        self.errors = set(errors or [])
        self.lookups = []

    def stat_object(self, object_name):
        self.lookups.append(object_name)
        if object_name in self.errors:
            raise StorageError(f"stat failed '{object_name}': connection reset")
        return self.stats.get(object_name)


def _artifact(key="reports/out.csv", size=12):
    return {"oss_key": key, "bytes": size}


def test_passes_when_every_artifact_exists_with_the_expected_size():
    storage = FakeStorage({"reports/out.csv": {"size": 12, "etag": "e"}})
    result = ArtifactValidator(storage).validate([_artifact()])

    assert result.ok
    assert result.checks[0].reason == "ok"
    assert result.checks[0].size == 12
    assert result.events() == [
        {"kind": "artifact_check", "oss_key": "reports/out.csv", "ok": True, "reason": "ok", "size": 12}
    ]


def test_no_artifacts_is_not_a_failure():
    result = ArtifactValidator(FakeStorage()).validate([])
    assert result.ok and result.checks == []


def test_missing_artifact_fails():
    result = ArtifactValidator(FakeStorage()).validate([_artifact()])
    assert not result.ok
    assert result.checks[0].reason == "missing in object storage"
    assert "reports/out.csv: missing" in result.failure_summary()


def test_empty_artifact_fails():
    storage = FakeStorage({"reports/out.csv": {"size": 0, "etag": "e"}})
    result = ArtifactValidator(storage).validate([_artifact(size=0)])
    assert not result.ok
    assert result.checks[0].reason == "empty object"


def test_size_mismatch_fails():
    storage = FakeStorage({"reports/out.csv": {"size": 99, "etag": "e"}})
    result = ArtifactValidator(storage).validate([_artifact(size=12)])
    assert not result.ok
    assert "size mismatch" in result.checks[0].reason


def test_storage_error_is_reported_not_raised():
    storage = FakeStorage(errors={"reports/out.csv"})
    result = ArtifactValidator(storage).validate([_artifact()])
    assert not result.ok
    assert "stat failed" in result.checks[0].reason


def test_artifact_without_key_fails_without_touching_storage():
    storage = FakeStorage()
    result = ArtifactValidator(storage).validate([{"bytes": 3}])
    assert not result.ok
    assert result.checks[0].reason == "artifact record has no oss_key"
    assert storage.lookups == []
