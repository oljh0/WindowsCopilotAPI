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
from .schemas import ChatCompletionRequest

import os
import hashlib
import json
proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")

_session_cache = {}
_cache_lock = threading.Lock()

def get_messages_fingerprint(messages: list) -> str:
    serialized = []
    for m in messages:
        role = getattr(m, 'role', None) or m.get('role', '')
        content = getattr(m, 'content', None) or m.get('content', '')
        if isinstance(content, list):
            text_parts = []
            for p in content:
                if isinstance(p, dict):
                    text_parts.append(p.get("text", ""))
                else:
                    text_parts.append(str(p))
            content_str = "\n".join(text_parts)
        else:
            content_str = str(content or "")
        serialized.append({"role": role, "content": content_str})
    data = json.dumps(serialized, sort_keys=True)
    return hashlib.md5(data.encode('utf-8')).hexdigest()

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


def _stream(prompt: str, model: str, conversation_id=None, messages=None, mode="smart"):
    """Yield OpenAI ``chat.completion.chunk`` SSE events for ``prompt``.

    ``conversation_id`` continues an existing Copilot thread; ``None`` starts a
    fresh one (its id is emitted on the final chunk).
    """
    if not getattr(_thread_local, "preheated", False):
        import curl_cffi.requests as requests
        try:
            res = requests.get("https://www.bing.com", impersonate="chrome", timeout=10)
            _thread_local.preheated = True
            print(f"[ThreadPreheat] Successfully preheated generator thread {threading.current_thread().name}: {res.status_code}")
        except Exception as e:
            print(f"[ThreadPreheat] Failed to preheat generator thread {threading.current_thread().name}: {e}")

    cid = new_id()
    created = int(time.time())
    reply_pieces = []
    try:
        with _upstream_lock:  # one upstream chat at a time (released on disconnect)
            yield sse_event(stream_chunk(cid, created, model, {"role": "assistant"}))
            stream = client.stream(prompt, conversation_id=conversation_id, mode=mode)
            for piece in stream:
                if isinstance(piece, str) and piece:
                    reply_pieces.append(piece)
                    yield sse_event(stream_chunk(cid, created, model, {"content": piece}))
            # Copilot's conversation id is known once the stream has run; emit it
            # on the final chunk so callers can track the upstream thread.
            actual_cid = stream.conversation_id
            yield sse_event(
                stream_chunk(
                    cid, created, model, {}, finish="stop",
                    conversation_id=actual_cid,
                )
            )
            
            # 缓存指纹映射
            if messages and actual_cid:
                full_reply = "".join(reply_pieces)
                from .schemas import ChatMessage
                updated_messages = messages + [ChatMessage(role="assistant", content=full_reply)]
                new_key = get_messages_fingerprint(updated_messages)
                with _cache_lock:
                    _session_cache[new_key] = actual_cid
                    if len(_session_cache) > 2000:
                        _session_cache.clear()
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


_thread_local = threading.local()


@app.post("/v1/chat/completions")
def chat_completions(req: ChatCompletionRequest):
    if not getattr(_thread_local, "preheated", False):
        import curl_cffi.requests as requests
        try:
            res = requests.get("https://www.bing.com", impersonate="chrome", timeout=10)
            _thread_local.preheated = True
            print(f"[ThreadPreheat] Successfully preheated thread {threading.current_thread().name}: {res.status_code}")
        except Exception as e:
            print(f"[ThreadPreheat] Failed to preheat thread {threading.current_thread().name}: {e}")

    model = req.model or MODEL_NAME
    
    # 映射模型到 mode
    mode = "smart"
    if "reasoning" in model or "thinking" in model:
        mode = "reasoning"
    elif "search" in model:
        mode = "search"
    elif "study" in model:
        mode = "study"

    # 确定会话 ID (Session 保持)
    req_conversation_id = req.conversation_id
    prompt_is_last_only = False
    
    if not req_conversation_id and len(req.messages) > 1:
        # 提取 messages[:-1] 指纹
        fingerprint = get_messages_fingerprint(req.messages[:-1])
        with _cache_lock:
            req_conversation_id = _session_cache.get(fingerprint)
        if req_conversation_id:
            prompt_is_last_only = True
            print(f"[SessionCache] Hit. Reusing conversation_id: {req_conversation_id}")

    # 如果是继续已有的 conversation_id，则 prompt 只发送最后一句话
    if req_conversation_id or prompt_is_last_only:
        from .prompt import content_text
        prompt = content_text(req.messages[-1].content)
    else:
        prompt = messages_to_prompt(req.messages)

    if not prompt.strip():
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "no text content in messages", "type": "invalid_request_error"}},
        )

    # Enforce the per-minute ceiling before touching the upstream lock, so excess
    # callers get a fast 429 instead of piling up behind the serialized queue.
    limited = _rate_limited_response()
    if limited is not None:
        return limited

    if req.stream:
        return StreamingResponse(
            _stream(prompt, model, req_conversation_id, req.messages, mode), media_type="text/event-stream"
        )

    try:
        with _upstream_lock:  # serialize: one upstream chat at a time
            reply = client.chat(prompt, conversation_id=req_conversation_id, mode=mode)
            
            # 非流式情况下的缓存记录
            if reply.conversation_id:
                updated_messages = req.messages + [ChatMessage(role="assistant", content=reply.text)]
                new_key = get_messages_fingerprint(updated_messages)
                with _cache_lock:
                    _session_cache[new_key] = reply.conversation_id
                    if len(_session_cache) > 2000:
                        _session_cache.clear()
    except Exception as exc:
        return JSONResponse(
            status_code=502,
            content={"error": {"message": str(exc), "type": "upstream_error"}},
        )
    return completion_response(reply.text, model, reply.conversation_id)


@app.get("/")
def root():
    return {"service": "Copilot OpenAI-compatible API", "endpoints": ["/v1/models", "/v1/chat/completions"]}
