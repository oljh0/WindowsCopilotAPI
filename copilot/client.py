"""High-level Copilot client — the recommended entry point.

One client, many conversations addressed by id. :meth:`CopilotClient.chat`
returns the full reply plus the conversation id; pass that id back to continue
the same conversation, or omit it to start a fresh one. :meth:`CopilotClient.stream`
is the incremental variant.

    from copilot import CopilotClient

    client = CopilotClient()                       # loads signed-in auth once
    r = client.chat("My name is Tomato. Remember it.")
    print(r.text, r.conversation_id)

    r2 = client.chat("What's my name?", r.conversation_id)   # continue
    print(r2.text)

    for chunk in client.stream("Tell me a joke"):  # new conversation, streamed
        print(chunk, end="", flush=True)

The signed-in access token is refreshed transparently; sign in once with
``python -m copilot login``. Pass ``anonymous=True`` to skip sign-in (only where
anonymous consumer chat is available), or ``proxy=...`` to route through a
supported region.
"""

import time
from dataclasses import dataclass, field
from typing import Generator, List, Optional, Union

from .auth import AUTH_MAX_AGE, load_auth
from .driver import Copilot
from .models import Conversation, ImageResponse


@dataclass
class ChatReply:
    """The full result of a :meth:`CopilotClient.chat` call."""

    text: str
    conversation_id: Optional[str]
    images: List[ImageResponse] = field(default_factory=list)


class ChatStream:
    """Iterable stream of reply chunks that also exposes the conversation id.

    Yields ``str`` text chunks (and :class:`~copilot.models.ImageResponse` for
    generated images). ``conversation_id`` is known up front when continuing an
    existing conversation, and is populated as soon as iteration begins when a
    new conversation is created.
    """

    def __init__(self, chunks: Generator, conversation_id: Optional[str]):
        self._chunks = chunks
        self.conversation_id = conversation_id

    def __iter__(self) -> Generator[Union[str, ImageResponse], None, None]:
        for item in self._chunks:
            if isinstance(item, Conversation):
                self.conversation_id = item.conversation_id
            else:
                yield item


class CopilotClient:
    """高层级 Copilot 客户端：对外部调用屏蔽底层网络质询和 Token 刷新细节。

    单个客户端对象即可管理多个不同 id 的会话。
    若调用 `stream()` 或 `chat()` 时提供 `conversation_id` 参数，则会沿用已有会话；
    若传入 `None`，则会重新初始化并在流的最初吐出新的 conversation 实例以供后续关联。

    参数
    ----------
    anonymous:
        若为 True 则代表以匿名方式发起请求。匿名会话在某些地理区域是受限的（如中国大陆、印度等）。
        默认 False 会自动去加载和周期性刷新已登录的微软账户凭证。
    proxy:
        全局代理配置字符串，如 "http://127.0.0.1:7890"，将同时应用于 Token 刷新和后续的所有请求。
    max_age:
        缓存凭证（MSAL 访问令牌）的可信任存活秒数（超时后会自动触发无头浏览器刷新）。
    """

    def __init__(
        self,
        anonymous: bool = False,
        proxy: Optional[str] = None,
        max_age: int = AUTH_MAX_AGE,
    ):
        self._driver = Copilot()
        self._anonymous = anonymous
        self._proxy = proxy
        self._max_age = max_age
        self._auth: Optional[dict] = None

    def stream(
        self,
        prompt: str,
        conversation_id: Optional[str] = None,
        mode: str = "smart",
        **kwargs,
    ) -> ChatStream:
        """流式获取微软 Copilot 对当前提示词 (prompt) 的回复，返回一个 ChatStream 迭代器。

        若 `conversation_id` 为 None 则是新对话，返回的 ChatStream 在迭代时会在首位吐出
        对应的 Conversation 实例，从而可以动态获取新会话的 conversation_id。
        """
        auth = self._fresh_auth()
        kw = dict(
            stream=True,
            proxy=self._proxy,
            cookies=auth["cookies"] if auth else None,
            access_token=auth["access_token"] if auth else None,
            mode=mode,
            **kwargs,
        )
        if conversation_id is None:
            # 新会话，令底层驱动在生成器首位返回 Conversation 对象
            kw["return_conversation"] = True
        else:
            kw["conversation_id"] = conversation_id

        chunks = self._driver.create_completion(prompt, **kw)
        return ChatStream(chunks, conversation_id)

    def chat(
        self,
        prompt: str,
        conversation_id: Optional[str] = None,
        mode: str = "smart",
        **kwargs,
    ) -> ChatReply:
        """非流式阻塞获取微软 Copilot 对当前提示词 (prompt) 的完整回复。

        常用于无需逐字打字机效果的单次交互。
        """
        s = self.stream(prompt, conversation_id=conversation_id, mode=mode, **kwargs)
        text: List[str] = []
        images: List[ImageResponse] = []
        for item in s:
            if isinstance(item, str):
                text.append(item)
            elif isinstance(item, ImageResponse):
                images.append(item)
        return ChatReply("".join(text), s.conversation_id, images)

    def _fresh_auth(self) -> Optional[dict]:
        """获取当前依然有效的登录态凭证，如已过期或未获取，则在后台拉起无头浏览器自动刷新。"""
        if self._anonymous:
            return None
        # 如果尚未加载过或者上次保存时间超过了指定的有效期，重新加载/刷新
        if self._auth is None or (time.time() - self._auth.get("saved_at", 0)) >= self._max_age:
            self._auth = load_auth(max_age=self._max_age, proxy=self._proxy)
        return self._auth
