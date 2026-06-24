"""FastAPI app wiring Copilot onto the OpenAI Chat Completions API."""

import threading
import time

from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse

from copilot import CopilotClient

from .config import MODEL_NAME, RATE_LIMIT_BURST, RATE_LIMIT_RPM
from .openai_format import (
    completion_response,
    new_id,
    sse_event,
    stream_chunk,
)
from .prompt import messages_to_prompt
from .ratelimit import TokenBucket
from .schemas import ChatCompletionRequest, ChatMessage

import os
import hashlib
import json
proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")

# 会话缓存字典，用于历史对话上下文的指纹匹配与 Session 保持
_session_cache = {}
# 长前缀缓存字典，用于模糊匹配单条超长 Prompt 发送场景下的 Session 保持（解决 Cline/OpenCode 将历史拼入单条消息的问题）
_prefix_history = {}
_cache_lock = threading.Lock()

# 线程局部变量，用于记录每个子线程是否已经完成 curl_cffi 的网络预热
_thread_local = threading.local()

def get_messages_fingerprint(messages: list) -> str:
    """根据除最后一条消息外的上下文消息生成 MD5 指纹，以便在后台复用 Copilot 的会话 ID (conversation_id)。"""
    serialized = []
    for m in messages:
        role = getattr(m, 'role', None) or m.get('role', '')
        content = getattr(m, 'content', None) or m.get('content', '')
        if isinstance(content, list):
            text_parts = []
            for p in content:
                if isinstance(p, dict):
                    t = p.get("type", "text")
                    if t == "text":
                        text_parts.append(p.get("text", ""))
                    elif t == "image_url":
                        img_url = p.get("image_url", {})
                        if isinstance(img_url, dict):
                            text_parts.append(f"[image:{img_url.get('url', '')}]")
                        else:
                            text_parts.append(f"[image:{img_url}]")
                    else:
                        text_parts.append(str(p))
                else:
                    text_parts.append(str(p))
            content_str = "\n".join(text_parts)
        else:
            content_str = str(content or "")
        serialized.append({"role": role, "content": content_str})
    # ensure_ascii=False 能够保证在包含中文等多字节字符时序列化表现的一致性
    data = json.dumps(serialized, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(data.encode('utf-8')).hexdigest()

def _preheat_thread():
    """在新分配的子线程首次调用 curl_cffi 前，提前执行一个 dummy 请求以防多线程同时初始化 libcurl 导致崩溃。"""
    if not getattr(_thread_local, "preheated", False):
        import curl_cffi.requests as requests
        thread_name = threading.current_thread().name
        print(f"[ThreadPreheat] Preheating thread {thread_name} (proxy={proxy})...")
        try:
            # 同样使用全局代理，缩短超时至 3 秒，若报错则优雅忽略，不阻断正常请求流程
            res = requests.get("https://copilot.microsoft.com", impersonate="chrome", timeout=3, proxy=proxy)
            _thread_local.preheated = True
            print(f"[ThreadPreheat] Successfully preheated thread {thread_name}: {res.status_code}")
        except Exception as e:
            print(f"[ThreadPreheat] Failed to preheat thread {thread_name} (non-fatal): {e}")

app = FastAPI(title="Copilot OpenAI-compatible API", version="1.0.0")
client = CopilotClient(proxy=proxy)

# Self-imposed rate limit on top of the concurrency lock below: this caps
# requests-per-minute, the lock caps requests-in-flight. See server/ratelimit.py.
_rate_limiter = TokenBucket(RATE_LIMIT_RPM, RATE_LIMIT_BURST)


def _rate_limited_response():
    """Spend a token; return an OpenAI-shaped 429 if none left, else ``None``."""
    allowed, wait = _rate_limiter.try_acquire()
    if allowed:
        return None
    secs = max(1, round(wait))
    return JSONResponse(
        status_code=429,
        headers={"Retry-After": str(secs)},
        content={"error": {
            "message": (
                f"Rate limit exceeded (>{RATE_LIMIT_RPM:g} req/min). "
                f"Retry in {secs}s."
            ),
            "type": "rate_limit_error",
            "code": "rate_limit_exceeded",
        }},
    )

# Copilot's per-account chat socket doesn't tolerate concurrent conversations
# from one process (parallel requests error out or hang). This server bridges a
# single signed-in account, so we serialize upstream calls: concurrent HTTP
# requests queue here and run one at a time. Predictable, at the cost of
# parallelism — fine for a personal bridge.
_upstream_lock = threading.Lock()


def _stream(prompt: str, model: str, conversation_id=None, messages=None, mode="smart", full_prompt=None):
    """使用 SSE 机制逐字返回 OpenAI 格式 the 流式块 (chat.completion.chunk)。"""
    _preheat_thread()

    cid = new_id()
    created = int(time.time())
    reply_pieces = []
    try:
        # 单账户串行锁，因为微软 Copilot 限制了单账户并发连接
        with _upstream_lock:
            yield sse_event(stream_chunk(cid, created, model, {"role": "assistant"}))
            stream = client.stream(prompt, conversation_id=conversation_id, mode=mode)
            for piece in stream:
                if isinstance(piece, str) and piece:
                    reply_pieces.append(piece)
                    yield sse_event(stream_chunk(cid, created, model, {"content": piece}))
            
            # 会话流式传输结束，保存其会话 ID 并在本层保存消息指纹以备下一轮请求匹配
            actual_cid = stream.conversation_id
            yield sse_event(
                stream_chunk(
                    cid, created, model, {}, finish="stop",
                    conversation_id=actual_cid,
                )
            )
            
            # 添加/更新会话上下文指纹与会话 ID 的缓存映射
            if messages and actual_cid:
                full_reply = "".join(reply_pieces)
                from .schemas import ChatMessage
                updated_messages = messages + [ChatMessage(role="assistant", content=full_reply)]
                new_key = get_messages_fingerprint(updated_messages)
                with _cache_lock:
                    _session_cache[new_key] = actual_cid
                    if len(_session_cache) > 2000:
                        # 仅淘汰最旧的 500 个，避免全部清空导致所有用户的对话同时丢失上下文
                        old_keys = list(_session_cache.keys())[:500]
                        for k in old_keys:
                            _session_cache.pop(k, None)
            
            # 添加/更新长前缀历史记录以备下一次模糊匹配
            if actual_cid and (full_prompt or prompt):
                fp = full_prompt or prompt
                # 如果当前是在已有前缀基础上进行的增量对话，我们应该记录“完整原始 prompt + 本轮回复”为下一次的前缀
                if full_prompt and len(reply_pieces) > 0:
                    fp = full_prompt + "\n" + "".join(reply_pieces)
                with _cache_lock:
                    _prefix_history[fp] = actual_cid
                    if len(_prefix_history) > 1000:
                        # 仅淘汰最旧的 300 个，避免全部清空
                        old_keys = list(_prefix_history.keys())[:300]
                        for k in old_keys:
                            _prefix_history.pop(k, None)
    except Exception as exc:  # surface errors to the client instead of hanging
        yield sse_event(
            stream_chunk(cid, created, model, {"content": f"\n[error: {exc}]"}, finish="error")
        )
    yield "data: [DONE]\n\n"


@app.get("/v1/models")
def list_models():
    models = [
        {"id": "copilot", "object": "model", "created": 0, "owned_by": "microsoft"},
        {"id": "copilot-smart", "object": "model", "created": 0, "owned_by": "microsoft"},
        {"id": "copilot-reasoning", "object": "model", "created": 0, "owned_by": "microsoft"},
        {"id": "copilot-thinking", "object": "model", "created": 0, "owned_by": "microsoft"},
        {"id": "copilot-search", "object": "model", "created": 0, "owned_by": "microsoft"},
        {"id": "copilot-study", "object": "model", "created": 0, "owned_by": "microsoft"}
    ]
    return {
        "object": "list",
        "data": models,
    }


@app.post("/v1/chat/completions")
def chat_completions(req: ChatCompletionRequest):
    """处理标准 OpenAI 风格的对话完成请求 (支持单次与流式返回)。"""
    _preheat_thread()

    model = req.model or MODEL_NAME
    
    # 将 OpenAI 模型名称映射到微软 Copilot 协议支持的 mode
    mode = "smart"
    if "reasoning" in model or "thinking" in model:
        mode = "reasoning"
    elif "search" in model:
        mode = "search"
    elif "study" in model:
        mode = "study"

    # 根据请求参数或上下文历史指纹匹配会话 ID (Session 保持)
    req_conversation_id = req.conversation_id
    prompt_is_last_only = False
    
    if not req_conversation_id and len(req.messages) > 1:
        # 提取当前请求中最后一条之前的消息列表，计算其上下文指纹
        fingerprint = get_messages_fingerprint(req.messages[:-1])
        with _cache_lock:
            req_conversation_id = _session_cache.get(fingerprint)
        if req_conversation_id:
            prompt_is_last_only = True
            print(f"[SessionCache] Hit. Reusing conversation_id: {req_conversation_id}")

    # 如果是继续已有的 conversation_id，则 prompt 只发送最后一句话，其余的通过 Copilot 服务器已建立的会话记忆恢复
    if req_conversation_id or prompt_is_last_only:
        from .prompt import content_text
        prompt = content_text(req.messages[-1].content)
        full_prompt = prompt
    else:
        prompt = messages_to_prompt(req.messages)
        full_prompt = prompt
        
        # 如果无法通过常规的多轮消息列表指纹定位会话，尝试通过本地长前缀哈希做模糊匹配
        if not req_conversation_id:
            with _cache_lock:
                # 优先匹配更长的前缀以获得更高精度
                sorted_history = sorted(_prefix_history.items(), key=lambda x: len(x[0]), reverse=True)
                for old_prompt, cid in sorted_history:
                    # 前缀匹配阈值设为 200 字符，防短指令冲突误匹配
                    if len(old_prompt) > 200 and prompt.startswith(old_prompt):
                        diff_prompt = prompt[len(old_prompt):].strip()
                        if diff_prompt:
                            req_conversation_id = cid
                            prompt = diff_prompt
                            prompt_is_last_only = True
                            print(f"[PrefixCache] Hit! Reusing conversation_id: {cid}. Stripped {len(old_prompt)} chars. Sending diff: {prompt[:100]}...")
                            break

    if not prompt.strip():
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "no text content in messages", "type": "invalid_request_error"}},
        )

    # 在请求发送给上游前先执行自 imposed 限流检查，防爆防刷
    limited = _rate_limited_response()
    if limited is not None:
        return limited

    # 流式（SSE）返回处理
    if req.stream:
        return StreamingResponse(
            _stream(prompt, model, req_conversation_id, req.messages, mode, full_prompt=full_prompt), media_type="text/event-stream"
        )

    # 非流式同步请求处理
    try:
        # 单账户串行锁，锁定上游交互
        with _upstream_lock:
            reply = client.chat(prompt, conversation_id=req_conversation_id, mode=mode)
            
            # 记录本轮回复后的完整消息历史指纹以备下一轮请求匹配
            if reply.conversation_id:
                updated_messages = req.messages + [ChatMessage(role="assistant", content=reply.text)]
                new_key = get_messages_fingerprint(updated_messages)
                with _cache_lock:
                    _session_cache[new_key] = reply.conversation_id
                    if len(_session_cache) > 2000:
                        # 淘汰最旧的 500 个，避免全部清空
                        old_keys = list(_session_cache.keys())[:500]
                        for k in old_keys:
                            _session_cache.pop(k, None)
                            
                # 同时将完整 prompt + 它的回复 记录进前缀匹配库，做下一次增量识别
                fp = full_prompt
                if reply.text:
                    fp = full_prompt + "\n" + reply.text
                print(f"[DEBUG] Caching prefix: fp_len={len(fp)}, cid={reply.conversation_id}")
                with _cache_lock:
                    _prefix_history[fp] = reply.conversation_id
                    if len(_prefix_history) > 1000:
                        # 仅淘汰最旧 of 300 个，避免全部清空
                        old_keys = list(_prefix_history.keys())[:300]
                        for k in old_keys:
                            _prefix_history.pop(k, None)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return JSONResponse(
            status_code=502,
            content={"error": {"message": str(exc), "type": "upstream_error"}},
        )
    return completion_response(reply.text, model, reply.conversation_id)


@app.get("/")
def root():
    return {"service": "Copilot OpenAI-compatible API", "endpoints": ["/v1/models", "/v1/chat/completions"]}
