"""Flatten an OpenAI ``messages`` array into a single Copilot prompt.

Copilot's protocol has no role/system channel — it takes one prompt string per
turn — so we collapse the whole conversation into one piece of text.
"""

from typing import Any, List, Optional, Union

from .schemas import ChatMessage


def content_text(content: Optional[Union[str, List[Any]]]) -> str:
    """Extract plain text from a message's content (string or content-parts)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for part in content:
        if isinstance(part, dict):
            if part.get("type") == "text":
                parts.append(part.get("text", ""))
        else:
            parts.append(str(part))
    return "\n".join(p for p in parts if p)


def messages_to_prompt(messages: List[ChatMessage], max_chars: int = 3800) -> str:
    """将 OpenAI 格式的 messages 数组扁平化拼接为单个 Copilot 提示词，并进行智能字数截断以避免 text-too-long 错误。"""
    system_parts = [content_text(m.content) for m in messages if m.role == "system" and m.content]
    system = "\n\n".join(system_parts)
    
    convo = [m for m in messages if m.role != "system"]
    
    # 给对话内容预留的最大剩余空间
    max_body_chars = max(500, max_chars - len(system) - 20)
    
    if len(convo) == 1 and convo[0].role == "user":
        body = content_text(convo[0].content)
        # 单条消息若超过最大空间，直接进行硬截断并附带提示
        if len(body) > max_body_chars:
            trunc_msg = "\n[...因字符长度限制，此处已截断...]"
            body = body[:max_body_chars - len(trunc_msg)] + trunc_msg
    else:
        # 多轮对话：从最新一条开始逆序拼装，直到超出剩余最大空间
        lines = []
        accumulated_len = 0
        truncated_any = False
        
        for m in reversed(convo):
            label = "User" if m.role == "user" else "Assistant"
            line = f"{label}: {content_text(m.content)}"
            potential_len = len(line) + (1 if lines else 0)
            
            if accumulated_len + potential_len > max_body_chars:
                truncated_any = True
                break
            
            lines.insert(0, line)
            accumulated_len += potential_len
            
        lines.append("Assistant:")  # 指引 Copilot 继续回复
        
        if truncated_any:
            # 在首部加入提示，告知 Copilot 历史消息已截断
            lines.insert(0, "[...由于输入长度限制，较早的历史上下文已被截断...]")
            
        body = "\n".join(lines)

    if system and body:
        return f"{system}\n\n{body}"
    return system or body
