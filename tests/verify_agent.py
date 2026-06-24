#!/usr/bin/env python3
"""
Agent 模块端到端自动化验证测试脚本。
自动创建临时文件，使用子进程拉起 agent.py 传入修改指令，读取修改后的文件进行多项断言校验，并测试备份恢复的安全容错。
"""

import os
import sys
import subprocess
import unittest
from pathlib import Path

# 添加项目根目录到模块路径
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT))

class TestAgentEndToEnd(unittest.TestCase):
    def setUp(self):
        # 定义测试文件路径
        self.test_file = PROJECT_ROOT / "tests" / "test_memo.txt"
        self.backup_file = PROJECT_ROOT / "tests" / "test_memo.txt.bak"
        
        # 初始写入内容
        self.initial_content = (
            "这是由自动化测试生成的临时测试文档。\n"
            "本文件用于验证 Copilot API 智能体 (Agent) 的文件读写与改写机制。\n"
            "目前文件还没有加入修改标记。\n"
        )
        
        # 每次测试前确保环境干净并写入初始测试正文
        if self.test_file.exists():
            os.remove(self.test_file)
        if self.backup_file.exists():
            os.remove(self.backup_file)
            
        self.test_file.write_text(self.initial_content, encoding="utf-8")
        print(f"\n[Test] 成功初始化临时测试文件: {self.test_file.name}")

    def tearDown(self):
        # 清理临时测试文件
        for f in (self.test_file, self.backup_file):
            if f.exists():
                os.remove(f)
        print("[Test] 成功清理临时测试文件。")

    def test_agent_successful_modification(self):
        """测试 Agent 在正常接收 Copilot 反馈时的修改流程与断言校验。"""
        # 注意：此处我们将直接运行 agent.py
        agent_path = PROJECT_ROOT / "agent.py"
        instruction = "请在文件首行追加 '【编辑人：Antigravity】'，并在第二行末尾追加 '（验证通过）'。"
        
        print(f"[Test] 启动 Agent 进行文件修改...")
        print(f"[Test] 指令: {instruction}")
        
        # 拉起子进程运行命令行工具，并捕获输出
        cmd = [
            sys.executable,
            str(agent_path),
            "-f", str(self.test_file),
            "-i", instruction,
            "-m", "copilot-smart"
        ]
        
        # 运行子进程，并显式传入当前环境变量以继承代理设置
        process = subprocess.run(cmd, env=os.environ, capture_output=True, text=True, encoding="utf-8")
        
        # 打印 Agent 运行的完整控制台输出，便于在测试日志中查看
        print("\n----- Agent 运行控制台日志开始 -----")
        print(process.stdout)
        if process.stderr:
            print("[Stderr Output]:", process.stderr)
        print("----- Agent 运行控制台日志结束 -----\n")
        
        # 断言 1：命令行进程应当成功退出 (Exit Code = 0)
        self.assertEqual(process.returncode, 0, f"Agent 命令行非正常退出，Exit Code: {process.returncode}")
        
        # 读取更新后的文件内容
        updated_content = self.test_file.read_text(encoding="utf-8")
        print("[Test] 修改后的文件实际内容为:\n---")
        print(updated_content)
        print("---")
        
        # 断言 2：文件内容应该包含我们指令中要求的内容
        self.assertIn("【编辑人：Antigravity】", updated_content, "测试失败：首行未成功追加编辑人信息")
        self.assertIn("（验证通过）", updated_content, "测试失败：第二行未成功追加验证通过字样")
        
        # 断言 3：备份文件 .bak 应当在成功运行后被自动清理
        self.assertFalse(self.backup_file.exists(), "测试失败：成功运行后备份文件未能自动删除")

    def test_agent_error_rollback_safety(self):
        """测试在输入非法参数或发生异常退出时，备份自动回滚机制能否确保原始文件不被损坏/变空。"""
        agent_path = PROJECT_ROOT / "agent.py"
        
        # 传入非法的代理设置，以强制制造与微软服务器交互时的网络连接失败异常，从而触发备份回滚
        instruction = "请随意修改。"
        cmd = [
            sys.executable,
            str(agent_path),
            "-f", str(self.test_file),
            "-i", instruction,
            "-m", "copilot-smart"
        ]
        
        # 强制将子进程的代理设为无效端口，使其交互必败
        bad_env = os.environ.copy()
        bad_env["HTTP_PROXY"] = "http://127.0.0.1:9999"
        bad_env["HTTPS_PROXY"] = "http://127.0.0.1:9999"
        
        print(f"[Test] 启动 Agent 测试安全回滚 (强制制造网络交互失败)...")
        process = subprocess.run(cmd, env=bad_env, capture_output=True, text=True, encoding="utf-8")
        
        # 断言 1：Agent 命令行应当以失败的退出码退出 (Exit Code != 0)
        self.assertNotEqual(process.returncode, 0, "测试失败：非法参数下 Agent 没有正常报错退出")
        
        # 读取文件内容，确认由于发生异常触发了回滚，原始内容依然完好无损
        current_content = self.test_file.read_text(encoding="utf-8")
        self.assertEqual(current_content, self.initial_content, "测试失败：发生异常后原始文件内容没有被正确回滚恢复，文件数据可能被损坏")
        print("[Test] 异常回滚安全性断言成功：原始测试文件内容没有发生任何改变。")

if __name__ == "__main__":
    unittest.main()
