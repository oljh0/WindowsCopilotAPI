#!/usr/bin/env python3
"""
Microsoft Copilot 本地文件编辑 Agent。
通过命令行读取本地文件，并结合自然语言修改指令，调用 Copilot 生成修改方案，最后安全地应用并更新回文件。
"""

import os
import sys
import argparse
import shutil
import re
import queue
from pathlib import Path
from typing import Tuple, Optional

# 将当前目录加入模块路径以确保能正常导入项目中的库
sys.path.append(str(Path(__file__).resolve().parent))

from copilot.client import CopilotClient, ImageResponse

def parse_args():
    parser = argparse.ArgumentParser(description="Microsoft Copilot File Editing Agent")
    parser.add_argument(
        "-f", "--file",
        required=True,
        help="目标要修改的文件路径 (例如: test_memo.txt)"
    )
    parser.add_argument(
        "-i", "--instruction",
        required=True,
        help="对该文件要进行的修改指令 (例如: '在末尾追加版权信息')"
    )
    parser.add_argument(
        "-m", "--model",
        default="copilot-smart",
        help="使用的 Copilot 模型模式 (可选: copilot-smart, copilot-reasoning, copilot-search, 默认: copilot-smart)"
    )
    return parser.parse_args()

def backup_file(file_path: Path) -> Path:
    """为原文件创建 .bak 备份文件。"""
    backup_path = file_path.with_suffix(file_path.suffix + ".bak")
    print(f"[Agent] 正在为原文件建立安全备份: {backup_path.name}")
    shutil.copy2(file_path, backup_path)
    return backup_path

def restore_backup(backup_path: Path, file_path: Path):
    """如果发生错误，从备份文件中还原原文件。"""
    if backup_path.exists():
        print(f"[Agent] [Warning] 检测到错误，正在从备份还原原文件...")
        shutil.move(str(backup_path), str(file_path))

def remove_backup(backup_path: Path):
    """清理备份文件。"""
    if backup_path.exists():
        os.remove(backup_path)

def extract_updated_content(reply_text: str) -> Tuple[str, str]:
    """
    智能解析 Copilot 的回复内容，从中提取出要写入文件的完整正文。
    返回: (提取出的正文内容, 提取使用的方法描述)
    """
    # 方法 1：优先尝试提取 markdown 代码块 (支持 ```text ... ``` 或 纯 ``` ... ```)
    code_blocks = re.findall(r"```(?:\w+)?\n(.*?)\n```", reply_text, re.DOTALL)
    if code_blocks:
        # 如果有多个代码块，选字符长度最长的一个（通常是修改后的完整新文件内容）
        longest_block = max(code_blocks, key=len)
        return longest_block.strip(), "Markdown 最长代码围栏提取"

    # 方法 2：尝试通过 XML 标签兼容匹配
    xml_match = re.search(r"<updated_content>(.*?)</updated_content>", reply_text, re.DOTALL | re.IGNORECASE)
    if xml_match:
        return xml_match.group(1).strip(), "XML 标签兼容匹配"

    # 方法 3：退化机制，直接取整个回复文本
    return reply_text.strip(), "兜底整文提取"

def run_agent():
    args = parse_args()
    file_path = Path(args.file).resolve()
    
    if not file_path.exists():
        print(f"[Agent] [Error] 目标文件不存在: {args.file}")
        sys.exit(1)

    # 1. 读取原文件内容
    try:
        content = file_path.read_text(encoding="utf-8")
        print(f"[Agent] 已成功读取目标文件: {file_path.name} (大小: {len(content)} 字符)")
    except Exception as e:
        print(f"[Agent] [Error] 读取文件失败: {e}")
        sys.exit(1)

    # 2. 建立安全备份
    backup_path = backup_file(file_path)

    # 3. 构造结构化 Prompt 指引 Copilot
    # 要求它扮演编辑智能体，输出完整内容，并利用 Markdown 代码块包裹
    prompt = f"""请帮我编辑一个文本文件。
目标文件的当前内容如下：
---
{content}
---

我的修改指令是：{args.instruction}

请直接输出修改后的完整文件新正文。请务必将完整的新正文包裹在 ```text 和 ``` 代码围栏之间。不要输出任何解释或多余的话。"""
    print(f"[Agent] 正在建立微软 Copilot 连接 (使用模型: {args.model})...")
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    client = CopilotClient(proxy=proxy)

    # 映射模型名称到微软协议底层支持的 mode 常量
    model_mode = "smart"
    if "reasoning" in args.model or "thinking" in args.model:
        model_mode = "reasoning"
    elif "search" in args.model:
        model_mode = "search"
    elif "study" in args.model:
        model_mode = "study"

    reply_pieces = []
    try:
        # 4. 发起请求并实时流式展示打字机效果
        stream = client.stream(prompt, mode=model_mode)
        print("[Copilot 回复流开始] >>>")
        for piece in stream:
            if isinstance(piece, str):
                reply_pieces.append(piece)
                # 实时输出到控制台
                sys.stdout.write(piece)
                sys.stdout.flush()
        print("\n<<< [Copilot 回复流结束]")
    except Exception as e:
        print(f"\n[Agent] [Error] 与 Copilot 交互失败: {e}")
        restore_backup(backup_path, file_path)
        sys.exit(1)

    full_reply = "".join(reply_pieces)
    if not full_reply.strip():
        print("[Agent] [Error] Copilot 返回了空回复，无法进行修改。")
        restore_backup(backup_path, file_path)
        sys.exit(1)

    # 5. 从回复中提取要写入的完整正文
    updated_content, method = extract_updated_content(full_reply)
    print(f"[Agent] 成功应用算法提取修改后正文 (提取方法: {method}, 大小: {len(updated_content)} 字符)")

    # 6. 安全写回原文件
    try:
        file_path.write_text(updated_content, encoding="utf-8")
        print(f"[Agent] [Success] 目标文件 '{file_path.name}' 已成功更新写回！")
        # 清理备份
        remove_backup(backup_path)
    except Exception as e:
        print(f"[Agent] [Error] 写回文件失败: {e}")
        restore_backup(backup_path, file_path)
        sys.exit(1)

if __name__ == "__main__":
    run_agent()
