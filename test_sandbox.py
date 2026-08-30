from agent_sandbox import Sandbox

# 1. 连接免密运行的沙箱容器
sandbox_client = Sandbox(
    base_url="http://localhost:8080"
)

# 2. 准备要在沙箱中运行的代码
code_to_run = """
import sys
import platform

print("Hello! 代码正在云沙箱内部安全运行...")
print(f"沙箱操作系统环境: {platform.platform()}")
print(f"Python 版本: {sys.version}")

with open('hello_sandbox.txt', 'w', encoding='utf-8') as f:
    f.write('这是在云沙箱内部生成的文件内容，非常安全！')

print("成功写入文件: hello_sandbox.txt")
"""

print("🚀 正在向云沙箱发送代码并执行...")
response = sandbox_client.jupyter.execute_code(code=code_to_run)

print("\n--- 沙箱返回结果 ---")
# 打印完整响应数据
if hasattr(response, 'data') and response.data:
    res_data = response.data
    # 尝试读取执行输出内容
    if hasattr(res_data, 'result'):
        print(res_data.result)
    elif hasattr(res_data, 'output'):
        print(res_data.output)
    else:
        print(res_data)
else:
    print(response)