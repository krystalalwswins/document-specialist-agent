import json
from openai import OpenAI
from hermes_hooks import HermesHookEngine
from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

# 实例化 Hermes Hook 引擎
engine = HermesHookEngine()

def run_code_tool(code: str) -> str:
    """提供给大模型调用的代码执行工具接口"""
    return engine.execute_in_sandbox(code)

def main():
    # 1. 前置准备：模拟上传一个待处理的文件到 MinIO OSS
    sample_csv = "Name,Department,Salary,Performance\nAlice,IT,15000,A\nBob,HR,8000,B\nCharlie,IT,18000,S\nDavid,IT,12000,B"
    engine.storage.upload_file_content("raw_data/employees.csv", sample_csv.encode('utf-8'))
    print("📋 初始测试数据已准备在 OSS: raw_data/employees.csv")

    # 2. 触发 Hermes Pre-Hook，将数据拉取至沙箱
    engine.pre_execution_hook(
        oss_key="raw_data/employees.csv", 
        sandbox_filename="input_employees.csv"
    )

    # 3. 初始化 LLM Client
    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)

    user_prompt = (
        "请读取沙箱中的 input_employees.csv 文件，"
        "筛选出 IT 部门且 Performance 为 A 或 S 的员工，"
        "计算他们的平均薪资并打印出来，最后把筛选出的数据保存为 /home/gem/workspace/high_performers.csv"
    )

    tools = [{
        "type": "function",
        "function": {
            "name": "run_code_tool",
            "description": "在云沙箱的隔离 Python 环境中安全执行代码，支持 pandas/numpy 等数据处理",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "要执行的 Python 代码"}
                },
                "required": ["code"]
            }
        }
    }]

    print(f"\n👤 [User Request]: {user_prompt}\n")

    # 4. LLM 思考并生成工具调用
    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": user_prompt}],
        tools=tools
    )

    message = response.choices[0].message
    if message.tool_calls:
        for tool_call in message.tool_calls:
            if tool_call.function.name == "run_code_tool":
                args = json.loads(tool_call.function.arguments)
                # 运行代码
                run_code_tool(args["code"])
                
                # 5. 触发 Hermes Post-Hook，将产物推回 OSS 并清洗沙箱
                download_url = engine.post_execution_hook(
                    sandbox_filename="high_performers.csv",
                    output_oss_key="reports/it_high_performers.csv"
                )
                print(f"\n🎉 生产全链路完成！结果安全下载地址:\n{download_url}")

if __name__ == "__main__":
    main()