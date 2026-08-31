"""End-to-end demo.

Prerequisites:
1. docker compose up -d          (start sandbox + MinIO)
2. .env with a valid LLM_API_KEY

Run: .venv\\Scripts\\python.exe demo/run_demo.py
"""

from __future__ import annotations

import json
import sys

from agent.wiring import build_orchestrator
from core.config import get_settings
from sandbox.client import SandboxClient
from sandbox.hooks import HermesHookEngine
from storage.storage_manager import StorageManager


SAMPLE_CSV = (
    "Name,Department,Salary,Performance\n"
    "Alice,IT,15000,A\n"
    "Bob,HR,8000,B\n"
    "Charlie,IT,18000,S\n"
    "David,IT,12000,B\n"
)


def main() -> None:
    settings = get_settings()
    if not settings.llm_api_key:
        print("请在 .env 中设置 LLM_API_KEY 后重试")
        sys.exit(1)

    storage = StorageManager(settings)
    sandbox = SandboxClient(settings)

    # 1. 把示例数据放进 OSS，再经 pre-hook 送入沙箱
    storage.upload_file_content("raw/employees.csv", SAMPLE_CSV.encode("utf-8"))
    engine = HermesHookEngine(settings=settings, sandbox=sandbox, storage=storage)
    engine.pre_execution_hook("raw/employees.csv", "input.csv")

    # 2. 运行 Agent
    orchestrator = build_orchestrator(settings)
    task = orchestrator.run(
        "读取沙箱中的 input.csv，筛选 IT 部门且 Performance 为 A 或 S 的员工，"
        "计算平均薪资，把结果用 save_report 保存为 reports/high_performers.csv，"
        "最后告诉我下载链接和平均薪资。"
    )

    print("\n=== 任务结果 ===")
    print(json.dumps(task.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
