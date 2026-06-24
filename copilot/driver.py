"""Pure-HTTP Copilot driver.

Speaks Microsoft Copilot's consumer chat protocol directly over a
Cloudflare-impersonating ``curl_cffi`` session — no browser required. This is the
low-level engine; most callers should use :class:`copilot.client.CopilotClient`.
See :mod:`copilot.browser` for the Playwright-backed fallback.
"""

import json
import time
import uuid
from select import select
from typing import Dict, Optional
from urllib.parse import quote

from curl_cffi.const import CurlECode, CurlInfo
from curl_cffi.curl import CurlError
from curl_cffi.requests import Session, CurlWsFlag

# curl_cffi's WebSocket.recv() loops on CURLE_AGAIN forever (select() then retry)
# and never returns on an idle socket, so we drive the fragment loop ourselves to
# honour a deadline. CURL_SOCKET_BAD is libcurl's "no active socket" sentinel.
_CURL_SOCKET_BAD = -1

from .challenges import solve_copilot_challenge, solve_hashcash
from .models import AbstractProvider, Conversation, ImageResponse, ImageType
from .protocol import CHAT_WEBSOCKET_URL, CONSENTS_FRAME, SET_OPTIONS_FRAME
from .utils import drain_json, is_accepted_format, raise_for_status, to_bytes


class Copilot(AbstractProvider):
    label = "Microsoft Copilot"
    url = "https://copilot.microsoft.com"
    working = True
    supports_stream = True
    default_model = "Copilot"
    needs_auth = False  # consumer chat works anonymously (cookies only)
    websocket_url = CHAT_WEBSOCKET_URL
    conversation_url = f"{url}/c/api/conversations"

    def create_completion(
            self,
            prompt: str,
            stream: bool = False,
            proxy: str = None,
            timeout: int = 900,
            image: ImageType = None,
            conversation: Optional[Conversation] = None,
            conversation_id: str = None,
            return_conversation: bool = False,
            cookies: Dict[str, str] = None,
            access_token: str = None,
            mode: str = "smart",
            **kwargs
        ):
        """Stream a Copilot reply to ``prompt``.

        Runs Copilot's own chat protocol over a Cloudflare-impersonating
        ``curl_cffi`` session: ``POST /c/api/conversations`` then a chat
        WebSocket (``send`` -> proof-of-work ``challenge`` -> ``appendText``* ->
        ``done``). The challenge is solved in-process (see
        :mod:`copilot.challenges`); no browser is required.

        ``prompt`` is the user message sent straight to the chat socket (the
        protocol has no separate system/role channel). Anonymous by default;
        pass ``cookies`` and/or ``access_token`` (e.g. exported from a signed-in
        browser session) to run as a logged-in user — required where anonymous
        consumer chat is region-restricted.

        Conversation targeting (first match wins):
          * ``conversation`` — reuse an existing :class:`Conversation` object;
          * ``conversation_id`` — resume a conversation by its id string (no
            create call), e.g. one saved from a previous run;
          * neither — create a fresh conversation. With ``return_conversation``
            the new :class:`Conversation` is yielded first.
        """
        # Resolve auth: explicit args win, else fall back to the conversation's.
        if cookies is None and conversation is not None:
            cookies = conversation.cookies
        if access_token is None and conversation is not None:
            access_token = conversation.access_token

        # Auth model mirrors the browser:
        #   * REST calls (conversation create, attachment upload) authenticate by
        #     COOKIE only. Sending the token as an Authorization: Bearer header
        #     there gets a 401 (browsers never do it), so we don't.
        #   * the chat WebSocket carries the signed-in identity via its
        #     ?accessToken= param. This must be the Copilot chat token (MSAL scope
        #     ChatAI.ReadWrite, selected in browser._FIND_TOKEN_JS): a
        #     wrong-audience token 401s the WS upgrade, while *no* token makes the
        #     chat backend treat the session as anonymous -> chat-service-
        #     unavailable in geo-restricted regions (e.g. India).
        # Mirror the real client's URL shape: api-version, then a fresh
        # per-connection clientSessionId, then the access token. The current chat
        # backend expects clientSessionId; omitting it is one trigger for an
        # `invalid-event` rejection.
        websocket_url = f"{self.websocket_url}&clientSessionId={uuid.uuid4()}"
        if access_token:
            websocket_url = f"{websocket_url}&accessToken={quote(access_token)}"

        with Session(
            timeout=timeout,
            proxy=proxy,
            impersonate="chrome",
            cookies=cookies,
        ) as session:
            # Establish cookies + Cloudflare clearance (anonymous is fine).
            session.get(f"{self.url}/")

            if conversation is not None:
                conversation_id = conversation.conversation_id
            elif conversation_id is not None:
                pass  # resume an existing conversation by id; skip create
            else:
                response = session.post(self.conversation_url)
                raise_for_status(response)
                conversation_id = response.json().get("id")
                if return_conversation:
                    yield Conversation(conversation_id, session.cookies.jar)

            images = []
            if image is not None:
                data = to_bytes(image)
                response = session.post(
                    f"{self.url}/c/api/attachments",
                    headers={"content-type": is_accepted_format(data)},
                    data=data,
                )
                raise_for_status(response)
                images.append({"type": "image", "url": response.json().get("url")})

            send_frame = json.dumps({
                "event": "send",
                "conversationId": conversation_id,
                "content": [*images, {"type": "text", "text": prompt}],
                "mode": mode,
                "context": {},
            }).encode()

            wss = session.ws_connect(websocket_url)
            # Initialise the session before sending: setOptions then
            # reportLocalConsents. A `send` issued first is rejected with
            # `invalid-event` (see the handshake constants above).
            wss.send(json.dumps(SET_OPTIONS_FRAME).encode(), CurlWsFlag.TEXT)
            wss.send(json.dumps(CONSENTS_FRAME).encode(), CurlWsFlag.TEXT)
            wss.send(send_frame, CurlWsFlag.TEXT)
            yield from self._read_stream(wss, send_frame, timeout)

    def _read_stream(self, wss, send_frame: bytes, timeout: int, idle_timeout: int = 60):
        """消费 Chat WebSocket 帧，自动求解微软的安全质询 PoW，并生成文本/图像回复。

        ``idle_timeout`` 限制了等待下一帧的空闲超时时长，因为上游通常在几秒内回复。
        若遇到长时间静默，通常意味着 socket 挂起或质询验证未通过，这会直接抛出超时异常。
        """
        buffer = b""
        is_started = False
        answered = False
        image_prompt = None
        last_msg = None
        err = None

        overall_deadline = time.time() + timeout
        while True:
            idle_deadline = time.time() + idle_timeout
            try:
                # 从 WebSocket 中阻塞读取一个完整的帧，或者在到达最严格的截止时间时返回 None
                chunk = self._recv_frame(wss, min(overall_deadline, idle_deadline))
            except Exception as e:
                err = e
                break  # 发生网络读取异常或 socket 被关闭，跳出接收循环
            
            if chunk is None:  # 到达超时截止时间但仍未收到任何帧
                if time.time() >= overall_deadline:
                    raise TimeoutError(f"Copilot 接收流式回复超过了最大时限 {timeout} 秒")
                raise TimeoutError(
                    f"Copilot 聊天套接字已连续空闲超过 {idle_timeout} 秒；"
                    f"收到的最后一个帧数据是：{last_msg!r}。"
                )

            buffer += chunk if isinstance(chunk, (bytes, bytearray)) else chunk.encode()
            messages, buffer = drain_json(buffer)
            for msg in messages:
                last_msg = msg
                event = msg.get("event")
                
                # 触发了微软的安全质询 (Proof of Work 挑战)
                if event == "challenge" and not answered:
                    token = self._solve_challenge(msg)
                    if token is None:
                        raise RuntimeError(
                            f"无法在纯 HTTP 模式下解密此 Copilot 安全验证 (方法={msg.get('method')!r})。"
                            "微软可能升级了质询要求，需要通过浏览器人机交互校验 (如 Turnstile)；"
                            "请尝试使用备用的 Playwright 浏览器驱动 (copilot.browser.BrowserCopilot)。"
                        )
                    # 回复安全质询帧
                    wss.send(json.dumps({
                        "event": "challengeResponse",
                        "token": token,
                        "method": msg.get("method"),
                        "id": msg.get("id"),
                    }).encode(), CurlWsFlag.TEXT)
                    answered = True
                    # 微软协议规定，在完成 challenge 校验后，客户端必须重新发送一遍被挂起的首条消息帧
                    wss.send(send_frame, CurlWsFlag.TEXT)
                    
                elif event == "appendText":
                    is_started = True
                    yield msg.get("text")
                    
                elif event == "generatingImage":
                    image_prompt = msg.get("prompt")
                    
                elif event == "imageGenerated":
                    yield ImageResponse(msg.get("url"), image_prompt, {"preview": msg.get("thumbnailUrl")})
                    
                elif event == "done":
                    # 回复正常结束，退出流读取
                    return
                    
                elif event == "error":
                    code = msg.get("errorCode") or msg
                    if code == "chat-service-unavailable":
                        raise RuntimeError(
                            "Copilot 报错: chat-service-unavailable。聊天服务在您当前的地理区域不可用；"
                            "如果您处于中国大陆等不受支持的地区，请务必设置全局代理或者为服务传入正确的代理参数，"
                            "例如: create_completion(..., proxy='http://user:pass@host:port')。"
                        )
                    raise RuntimeError(f"Copilot 服务端报错: {code}")

        # 如果连接在正常接收文本前就被异常挂断
        if not is_started:
            if err is not None:
                raise RuntimeError(
                    f"与 Copilot 建立 WebSocket 连接并在开始接收回复前遇到致命网络异常: {err}"
                ) from err
            raise RuntimeError(f"无效响应：未收到任何流式回复文本。最后接收到的帧数据：{last_msg}")

    @staticmethod
    def _recv_frame(wss, deadline: float):
        """阻塞并读取一个完整的 WebSocket 帧，直到超时截止时间（时间戳秒）则返回 None。

        由于 curl_cffi 底层的 `recv_fragment()` 遇到没有数据时会持续返回 `CURLE_AGAIN`，
        本方法自行驱动分片循环，在遇到 EAGAIN 错误时使用 `select` 监听套接字进行非阻塞睡眠以响应截止时间，
        从而防止对端由于死锁或断线导致 Python 进程无限挂起。
        """
        sock_fd = wss.curl.getinfo(CurlInfo.ACTIVESOCKET)
        if sock_fd == _CURL_SOCKET_BAD:
            raise ConnectionError("WebSocket 没有可用的活跃底层套接字")
        chunks = []
        while True:
            try:
                # 尝试拉取 WS 分段片段
                chunk, frame = wss.recv_fragment()
                chunks.append(chunk)
                # 当且仅当剩余分片为 0 且当前帧没有后续 CONT 标志时，才代表本帧接收完毕
                if frame.bytesleft == 0 and frame.flags & CurlWsFlag.CONT == 0:
                    return b"".join(chunks)
            except CurlError as e:
                if e.code != CurlECode.AGAIN:
                    raise  # 抛出非 EAGAIN 的致命连接错误
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None  # 到达指定的截止超时时间
                # 在套接字上进行短时间的 select 监听，防止死循环空转爆 CPU
                select([sock_fd], [], [], min(0.5, remaining))

    @staticmethod
    def _solve_challenge(msg: dict):
        """自动求解安全质询，若无法求解（如需要浏览器端 CAPTCHA）则返回 None。

        如果是空的 challenge（即没有任何 method），返回空字符串进行简单应答确认即可。
        如果是 `hashcash` 算法，在本地调用哈希前缀碰撞求解；
        如果是 `copilot` 算术公式，执行本地算术解析；
        如果是 `cloudflare` (Turnstile)，由于纯 HTTP 驱动无法直接模拟人机验证，将返回 None 由调用方决策。
        """
        method = msg.get("method")
        parameter = msg.get("parameter")
        if not method and not parameter:
            return ""  # 无操作/空质询：直接应答确认
        if method == "hashcash" and parameter:
            return solve_hashcash(parameter)
        if method == "copilot" and parameter:
            return solve_copilot_challenge(parameter)
        # cloudflare (Turnstile) 或未知的强验证方法，需降级回 BrowserCopilot 解决
        return None
