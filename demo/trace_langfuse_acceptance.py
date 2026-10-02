"""T5 acceptance: real document task with local trace + opt-in Langfuse export.

Runs the production wiring (one-shot container backend) through the API
lifespan, so the export thread is live exactly like a deployed API. Waits for
the root operation to close, settles every export receipt, then reads the
observation set back through the Langfuse public API and compares it with the
local ended set. Never prints credentials.

Usage: .venv/Scripts/python.exe -m demo.trace_langfuse_acceptance
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from api.app import create_app
from core.config import get_settings
from storage.storage_manager import StorageManager

POLL_SECONDS = 5.0
TASK_DEADLINE_SECONDS = 420.0
TRACE_DEADLINE_SECONDS = 60.0
EXPORT_DEADLINE_SECONDS = 90.0
TERMINAL = {"accepted", "uncertain", "rejected", "exhausted"}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--export-deadline", type=float, default=EXPORT_DEADLINE_SECONDS,
        help="seconds to wait for every ended observation to reach a terminal "
             "export receipt (the exporter sends roughly one span per second)",
    )
    return parser.parse_args(argv)

CSV_BODY = (
    "region,product,revenue\n"
    "north,widget,1200\n"
    "south,widget,800\n"
    "north,gadget,300\n"
    "east,widget,450\n"
    "south,gadget,150\n"
)

USER_INPUT = (
    "归档任务：sandbox 里已有一份 sales.csv。请解析它，按 region 汇总 revenue，"
    "把结果写成 CSV 文件 reports/region_summary.csv 并保存产物，"
    "最后在回答里给出总收入以及每个 region 的小计。"
)


def stdout_utf8() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _auth_headers(settings) -> dict[str, str]:
    public = settings.langfuse_public_key.get_secret_value()
    secret = settings.langfuse_secret_key.get_secret_value()
    token = base64.b64encode(f"{public}:{secret}".encode()).decode()
    return {"Authorization": "Basic " + token}


def remote_observations(settings, trace_id: str, window_start: datetime) -> dict:
    """Page the v4 observations read API and keep only this trace's spans."""
    base = settings.langfuse_base_url.rstrip("/")
    query = {
        "fromStartTime": (window_start - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "toStartTime": (datetime.now(timezone.utc) + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": "100",
    }
    headers = _auth_headers(settings)
    ids: list[str] = []
    cursor = None
    http_status = None
    error = None
    for _ in range(10):
        params = dict(query)
        if cursor:
            params["cursor"] = cursor
        url = f"{base}/api/public/v2/observations?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                http_status = response.status
                body = json.loads(response.read(2_000_000) or b"{}")
        except urllib.error.HTTPError as exc:
            http_status, error = exc.code, exc.read(400).decode("utf-8", "replace")
            break
        except Exception as exc:  # noqa: BLE001 - report, never crash the acceptance
            error = type(exc).__name__
            break
        rows = body.get("data") or []
        ids.extend(row["id"] for row in rows if row.get("traceId") == trace_id)
        cursor = (body.get("meta") or {}).get("cursor")
        if not cursor or len(rows) < int(query["limit"]):
            break
    return {"http": http_status, "error": error, "ids": ids}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    stdout_utf8()
    settings = get_settings()
    print("model        :", settings.llm_model)
    print("llm key set  :", bool(settings.llm_api_key), "(value never printed)")
    print("trace        :", settings.trace_enabled, "store:", settings.trace_store_dir)
    print("langfuse     :", settings.langfuse_enabled, "env:", settings.langfuse_environment)
    print("langfuse host:", settings.langfuse_base_url)
    print("keys present :", bool(settings.langfuse_public_key.get_secret_value()),
          bool(settings.langfuse_secret_key.get_secret_value()))
    print()

    run_id = uuid.uuid4().hex[:8]
    oss_key = f"raw/t5-{run_id}-sales.csv"
    storage = StorageManager(settings)
    try:
        url = storage.upload_file_content(oss_key, CSV_BODY.encode("utf-8"))
        print("staged input :", f"{oss_key} ({url.split('?')[0]})")
    except Exception as exc:  # noqa: BLE001
        print("FAIL: cannot stage input:", type(exc).__name__, exc)
        return 2

    app = create_app()
    window_start = datetime.now(timezone.utc)
    started = time.monotonic()
    with TestClient(app) as client:
        submitted = client.post(
            "/tasks",
            json={
                "user_input": USER_INPUT,
                "input_files": [{"oss_key": oss_key, "filename": "sales.csv"}],
                "require_artifact": True,
            },
        )
        if submitted.status_code != 201:
            print("FAIL: submit rejected:", submitted.status_code, submitted.text[:300])
            return 1
        task_id = submitted.json()["id"]
        print("task id      :", task_id)

        payload: dict = {}
        deadline = time.monotonic() + TASK_DEADLINE_SECONDS
        while time.monotonic() < deadline:
            payload = client.get(f"/tasks/{task_id}").json()
            if payload.get("status") in {"SUCCESS", "FAILED"}:
                break
            time.sleep(POLL_SECONDS)
        elapsed = time.monotonic() - started

        trace = client.get(f"/tasks/{task_id}/trace")
        trace_ok = trace.status_code == 200
        trace_body = trace.json() if trace_ok else {}
        trace_id = (trace_body.get("manifest") or {}).get("trace_id")

        # The root operation only closes after the worker returns, so wait for it
        # before judging the export set.
        trace_deadline = time.monotonic() + TRACE_DEADLINE_SECONDS
        while trace_ok and trace_body.get("capture_status") != "COMPLETE":
            if time.monotonic() > trace_deadline:
                break
            time.sleep(1.0)
            trace_body = client.get(f"/tasks/{task_id}/trace").json()

        export_body: dict = {}
        if trace_ok:
            export_deadline = time.monotonic() + args.export_deadline
            while True:
                export_body = client.get(f"/tasks/{task_id}/trace/export").json()
                receipts = (export_body.get("export") or {}).get("observations") or {}
                ended = _ended_ids(trace_body)
                unsettled = [oid for oid in ended
                             if receipts.get(oid, {}).get("status", "pending") not in TERMINAL]
                if not unsettled and len(receipts) >= len(ended):
                    break
                if time.monotonic() > export_deadline:
                    break
                time.sleep(settings.langfuse_export_interval)
                trace_body = client.get(f"/tasks/{task_id}/trace").json()

        remote = remote_observations(settings, trace_id, window_start) if trace_id else {}

    report = {
        "run_id": run_id, "task_id": task_id, "trace_id": trace_id,
        "elapsed_s": round(elapsed, 1), "status": payload.get("status"),
        "task": payload, "trace": trace_body, "export": export_body, "remote": remote,
    }
    out_path = Path(".data") / "trace-e2e" / f"{run_id}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print("status       :", payload.get("status"))
    print("elapsed_s    : %.1f" % elapsed)
    print("trace status :", "ok" if trace_ok else f"HTTP {trace.status_code}")
    print("trace id     :", trace_id)
    print("capture      :", trace_body.get("capture_status"))
    _print_local(trace_body)
    _print_export(export_body)
    match = _print_remote(remote, trace_body)

    answer = ((payload.get("result") or {}).get("answer") or "")
    print()
    print("final answer :", answer[:400].replace("\n", " ") or "(none)")
    artifacts = (payload.get("result") or {}).get("artifacts") or []
    print("artifacts    :", len(artifacts))
    for artifact in artifacts:
        print("  -", artifact.get("oss_key"), artifact.get("bytes"), "bytes")
    print("raw report   :", out_path)

    if payload.get("error"):
        print("failure      :", str(payload["error"])[:400])

    print()
    print("=== decision ===")
    export_ok = _export_verdict(export_body, trace_body)
    business = payload.get("status") or "UNKNOWN"
    capture = trace_body.get("capture_status") if trace_ok else "NO_TRACE"
    ok = (
        business == "SUCCESS"
        and bool(artifacts)
        and capture == "COMPLETE"
        and export_ok == "complete"
        and match in {"match", "remote-unavailable"}
    )
    print(f"business={business} local_trace={capture} export_receipts={export_ok} remote={match}")
    print("business + local trace + export receipts verified." if ok
          else "chain did NOT fully complete; see receipts above.")
    return 0 if ok else 1


def _ended_ids(trace_body: dict) -> list[str]:
    return [event["observation_id"] for event in trace_body.get("events", [])
            if event.get("event") == "ended"]


def _print_local(trace_body: dict) -> None:
    events = trace_body.get("events", [])
    started = {e["observation_id"]: e for e in events if e.get("event") == "started"}
    ended = _ended_ids(trace_body)
    kinds = Counter(started[oid]["type"] for oid in ended if oid in started)
    print("local ops    : %d ended %s" % (len(ended), dict(sorted(kinds.items()))))
    generations = [e for e in events
                   if e.get("event") == "ended" and e.get("type") == "generation"]
    if generations:
        sample = generations[0].get("metadata", {})
        print("generations  : %d, usage keys=%s, model=%s"
              % (len(generations), sorted((sample.get("usage") or {}).keys()), sample.get("model")))
    tools = [e for e in events
             if e.get("event") == "ended" and e.get("type") == "tool"]
    print("tool ops     : %d -> %s" % (len(tools), sorted({e.get("name") for e in tools})))
    root = (trace_body.get("manifest") or {}).get("root_id")
    root_end = next((e for e in events
                     if e.get("event") == "ended" and e.get("observation_id") == root), None)
    if root_end:
        print("root output  :", json.dumps(root_end.get("output"), ensure_ascii=False)[:200])


def _print_export(export_body: dict) -> None:
    state = export_body.get("export") or {}
    receipts = state.get("observations") or {}
    counts = Counter(receipt.get("status") for receipt in receipts.values())
    print("export       : enabled=%s disabled_reason=%s destination_matches=%s"
          % (export_body.get("enabled"), export_body.get("disabled_reason"),
             export_body.get("destination_matches")))
    print("receipts     : %d %s" % (len(receipts), dict(counts)))


def _print_remote(remote: dict, trace_body: dict) -> str:
    if not remote:
        return "remote-unavailable"
    local = set(_ended_ids(trace_body))
    ids = remote.get("ids") or []
    if remote.get("http") != 200:
        print("remote obs   : http=%s error=%s" % (remote.get("http"), str(remote.get("error"))[:200]))
        return "remote-unavailable"
    remote_ids = set(ids)
    missing = sorted(local - remote_ids)
    extra = sorted(remote_ids - local)
    print("remote obs   : http=200 count=%d local_ended=%d missing=%d extra=%d"
          % (len(remote_ids), len(local), len(missing), len(extra)))
    if missing:
        print("remote missing ids:", missing[:10])
    if extra:
        print("remote extra ids  :", extra[:10])
    return "match" if not missing else "missing"


def _export_verdict(export_body: dict, trace_body: dict) -> str:
    state = export_body.get("export") or {}
    if export_body.get("disabled_reason"):
        return "disabled:" + str(export_body["disabled_reason"])
    receipts = state.get("observations") or {}
    ended = _ended_ids(trace_body)
    if not ended:
        return "no-local-operations"
    accepted = sum(1 for oid in ended if receipts.get(oid, {}).get("status") == "accepted")
    if accepted == len(ended):
        return "complete"
    counts = Counter(receipts.get(oid, {}).get("status", "pending") for oid in ended)
    return "accepted=%d/%d %s" % (accepted, len(ended), dict(counts))


if __name__ == "__main__":
    sys.exit(main())
