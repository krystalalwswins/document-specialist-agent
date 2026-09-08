"""Real LLM + sandbox + MinIO example. Set LLM_API_KEY and start Docker first."""
import base64
import json
from agent.wiring import build_orchestrator
from core.config import get_settings

SAMPLE_CSV = 'Name,Department,Salary,Performance\nAlice,IT,15000,A\nBob,HR,8000,B\nCharlie,IT,18000,S\nDavid,IT,12000,B\n'


def main():
    settings = get_settings()
    if not settings.llm_api_key:
        raise SystemExit('请在 .env 中设置 LLM_API_KEY 后重试')
    task = build_orchestrator(settings).run(
        '读取 input.csv，筛选 IT 部门且 Performance 为 A 或 S 的员工，'
        '生成 high_performers.csv，保留 Name,Salary 两列，用 save_report 保存；最后给出平均薪资和下载链接。',
        inputs=[{'filename': 'input.csv', 'content_base64': base64.b64encode(SAMPLE_CSV.encode()).decode()}],
        artifact_requirements={'required': True, 'format': 'csv', 'required_columns': ['Name', 'Salary'], 'min_rows': 2})
    print(json.dumps(task.to_dict(), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
