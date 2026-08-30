from agent_sandbox import Sandbox
from storage_manager import StorageManager
from config import SANDBOX_BASE_URL, SANDBOX_WORKSPACE

class HermesHookEngine:
    """
    Hermes Lifecycle Engine:
    负责在 Agent 执行代码前装载数据(Pre-Hook)，在代码执行后持久化产物并清理沙箱(Post-Hook)。
    """
    def __init__(self):
        self.sandbox = Sandbox(base_url=SANDBOX_BASE_URL)
        self.storage = StorageManager()

    def pre_execution_hook(self, oss_key: str, sandbox_filename: str):
        """Pre-Hook: 阻断式文件同步 (OSS -> 云沙箱)"""
        print(f"⚓ [Hermes Pre-Hook] 正在从对象存储拉取 [{oss_key}]...")
        file_bytes = self.storage.download_file_content(oss_key)
        
        sandbox_path = f"{SANDBOX_WORKSPACE}/{sandbox_filename}"
        # 将文件安全写入云沙箱隔离目录
        self.sandbox.file.write_file(file=sandbox_path, content=file_bytes.decode('utf-8'))
        print(f"✅ [Hermes Pre-Hook] 文件已就绪于沙箱隔离路径: {sandbox_path}")

    def post_execution_hook(self, sandbox_filename: str, output_oss_key: str) -> str:
        """Post-Hook: 产物落盘与无状态沙箱清洗 (云沙箱 -> OSS)"""
        print(f"⚓ [Hermes Post-Hook] 正在提取沙箱产物 [{sandbox_filename}]...")
        sandbox_path = f"{SANDBOX_WORKSPACE}/{sandbox_filename}"
        
        # 1. 提取产物
        res = self.sandbox.file.read_file(file=sandbox_path)
        content_bytes = res.data.content.encode('utf-8')
        
        # 2. 刷盘保存至对象存储
        download_url = self.storage.upload_file_content(output_oss_key, content_bytes)
        print(f"✅ [Hermes Post-Hook] 产物已安全持久化至 OSS!")
        
        # 3. 清洗沙箱临时文件 (保持无状态性)
        clean_code = f"import os; os.remove('{sandbox_path}') if os.path.exists('{sandbox_path}') else None"
        self.sandbox.jupyter.execute_code(code=clean_code)
        print(f"🧹 [Hermes Post-Hook] 沙箱环境已自动完成状态清洗。")
        
        return download_url

    def execute_in_sandbox(self, code: str) -> str:
        """在隔离沙箱中安全执行 Agent 生成的代码"""
        print("🚀 [Sandbox Execution] 正在沙箱内部安全运行代码...")
        response = self.sandbox.jupyter.execute_code(code=code)
        
        logs = []
        if hasattr(response, 'data') and response.data:
            outputs = getattr(response.data, 'outputs', [])
            for out in outputs:
                if out.output_type == 'stream':
                    logs.append(out.text)
                elif out.output_type == 'error':
                    logs.append(f"Execution Error: {out.ename}: {out.evalue}\n{out.traceback}")
        
        result_text = "".join(logs) if logs else "执行成功（无控制台输出）"
        print(f"📦 [Sandbox Output]\n{result_text}")
        return result_text