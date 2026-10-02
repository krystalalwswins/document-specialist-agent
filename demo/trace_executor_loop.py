"""Trace what the model actually returns each Executor iteration.

Runs the same E2E task but wraps the Executor's LLM client, recording
finish_reason / content / tool_calls per round. Read-only w.r.t. the codebase.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from agent.wiring import build_orchestrator
from api.app import create_app
from core.config import get_settings
from storage.storage_manager import StorageManager

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


class Recorder:
    def __init__(self, inner, log):
        self._inner = inner
        self._log = log

    def chat(self, messages, tools=None, tool_choice=None, on_event=None):
        response = self._inner.chat(
            messages, tools=tools, tool_choice=tool_choice, on_event=on_event
        )
        choice = response.choices[0]
        message = choice.message
        calls = []
        for call in message.tool_calls or []:
            calls.append(
                {
                    "name": call.function.name,
                    "args": (call.function.arguments or "")[:240],
                }
            )
        self._log.append(
            {
                "messages": len(messages),
                "finish_reason": getattr(choice, "finish_reason", None),
                "content": (message.content or "")[:400],
                "tool_calls": calls,
            }
        )
        return response


def main() -> int:
    settings = get_settings()
    run_id = uuid.uuid4().hex[:8]
    oss_key = "raw/trace-%s-sales.csv" % run_id
    storage = StorageManager(settings)
    storage.upload_file_content(oss_key, CSV_BODY.encode("utf-8"))
    print("staged:", oss_key)

    orchestrator = build_orchestrator(settings)
    log: list[dict] = []
    executor = getattr(orchestrator, "_executor")
    executor._llm = Recorder(executor._llm, log)

    app = create_app(orchestrator, settings)
    with TestClient(app) as client:
        resp = client.post(
            "/tasks",
            json={
                "user_input": USER_INPUT,
                "input_files": [{"oss_key": oss_key, "filename": "sales.csv"}],
                "require_artifact": True,
            },
        )
        task_id = resp.json()["id"]
        deadline = time.monotonic() + 400
        payload = {}
        while time.monotonic() < deadline:
            payload = client.get("/tasks/%s" % task_id).json()
            if payload.get("status") in {"SUCCESS", "FAILED"}:
                break
            time.sleep(4)

    out = Path(".data") / "trace" / ("%s.json" % run_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"trace": log, "task": payload}, ensure_ascii=False, indent=2), encoding="utf-8")

    print("status:", payload.get("status"), "| llm rounds:", len(log), "| trace:", out)
    print()
    for index, entry in enumerate(log, start=1):
        print(
            "--- round %d | finish=%s | messages=%s"
            % (index, entry["finish_reason"], entry["messages"])
        )
        if entry["content"]:
            print("    content:", entry["content"].replace("\n", " ")[:200])
        if not entry["tool_calls"]:
            print("    tool_calls: (none -- loop should have returned here)")
        for call in entry["tool_calls"]:
            print("    call    : %s %s" % (call["name"], call["args"].replace("\n", " ")[:200]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
