"""Real end-to-end acceptance: LLM -> parse -> code -> artifact -> answer.

Uses the production wiring (one-shot container backend) and the real LLM from
.env. Never prints the API key. Writes the raw task JSON to .data/real-e2e/.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from api.app import create_app
from core.config import get_settings
from storage.storage_manager import StorageManager

POLL_SECONDS = 5.0
DEADLINE_SECONDS = 420.0

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


def main() -> int:
    settings = get_settings()
    print("model      :", settings.llm_model)
    print("api_key set:", bool(settings.llm_api_key), "(value never printed)")
    print("sandbox    : one-shot containers, image pinned by digest")
    print()

    run_id = uuid.uuid4().hex[:8]
    oss_key = "raw/e2e-%s-sales.csv" % run_id
    storage = StorageManager(settings)
    try:
        url = storage.upload_file_content(oss_key, CSV_BODY.encode("utf-8"))
        print("staged input : %s (%s)" % (oss_key, url.split("?")[0]))
    except Exception as exc:
        print("FAIL: cannot stage input into object storage: %s: %s" % (type(exc).__name__, exc))
        return 2

    app = create_app()
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
            print("FAIL: submit rejected: %s %s" % (submitted.status_code, submitted.text[:300]))
            return 1
        task_id = submitted.json()["id"]
        print("task id      : %s" % task_id)

        payload = {}
        deadline = time.monotonic() + DEADLINE_SECONDS
        while time.monotonic() < deadline:
            payload = client.get("/tasks/%s" % task_id).json()
            if payload.get("status") in {"SUCCESS", "FAILED"}:
                break
            time.sleep(POLL_SECONDS)
        elapsed = time.monotonic() - started

    output_path = Path(".data") / "real-e2e" / ("%s.json" % run_id)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    status = payload.get("status")
    print()
    print("status       : %s" % status)
    print("elapsed_s    : %.1f" % elapsed)
    print("raw output   : %s" % output_path)

    metrics = payload.get("metrics") or {}
    usage = metrics.get("usage") or {}
    task_usage = usage.get("task") or {}
    if task_usage:
        print(
            "tokens       : prompt=%s completion=%s total=%s"
            % (
                task_usage.get("prompt_tokens"),
                task_usage.get("completion_tokens"),
                task_usage.get("total_tokens"),
            )
        )
    for phase, phase_usage in (usage.get("by_phase") or {}).items():
        print(
            "  phase %-18s total_tokens=%s latency_ms=%s"
            % (phase, phase_usage.get("total_tokens"), phase_usage.get("latency_ms"))
        )

    result = payload.get("result") or {}
    answer = result.get("answer") or result.get("final_answer") or ""
    print("final answer : %s" % (answer[:400].replace("\n", " ") or "(none)"))

    artifacts = result.get("artifacts") or []
    print("artifacts    : %d" % len(artifacts))
    for artifact in artifacts:
        print("  - %s (%s bytes)" % (artifact.get("oss_key"), artifact.get("bytes")))

    if payload.get("error"):
        print("failure      : %s" % str(payload["error"])[:400])

    print()
    print("=== decision ===")
    if status == "SUCCESS" and artifacts:
        print("Full chain held: parse + code + artifact commit + final answer.")
        return 0
    print("Chain did NOT complete as required.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
