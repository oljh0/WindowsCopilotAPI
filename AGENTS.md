# AGENTS.md

This file provides guidance to Qoder (qoder.com) when working with code in this repository.

## Project Overview

Windows Copilot API — a zero-cost, local OpenAI-compatible API bridge for Microsoft Copilot. It converts a personal Microsoft Copilot web session into a standard OpenAI Chat Completions endpoint (`/v1/chat/completions`), supporting SSE streaming, multi-turn conversation session persistence (via fingerprint caching), and multiple Copilot modes (smart, reasoning, thinking, search, study).

Python 3.9+. Dependencies: `curl_cffi`, `playwright`, `fastapi`, `uvicorn`.

## Common Commands

```bash
# Install dependencies
pip install -r requirements.txt
playwright install chromium

# First-time login (opens a visible browser window for Microsoft sign-in)
python -m copilot login

# Start the API server (default: http://127.0.0.1:8000)
python app.py

# Custom host/port
uvicorn server.api:app --host 0.0.0.0 --port 8080

# One-shot CLI query (uses the browser driver)
python -m copilot ask "your prompt here"

# Docker (requires login on host first)
docker compose up --build
```

Proxy env vars (`HTTP_PROXY` / `HTTPS_PROXY`) must be set before `python app.py` if behind a proxy. Rate limit is configured via `RATE_LIMIT_RPM` and `RATE_LIMIT_BURST` env vars (see `server/config.py`).

## Architecture

```
app.py                  Entry point. Calls server.app() which starts uvicorn.

copilot/                Core library — talks to Microsoft Copilot.
  client.py             High-level API: CopilotClient with chat() and stream().
                        Handles auth refresh automatically.
  driver.py             Low-level pure-HTTP driver using curl_cffi. Speaks the
                        Copilot WebSocket protocol directly (no browser needed).
                        This is the primary path used by the server.
  browser.py            Playwright-backed fallback driver (BrowserCopilot).
                        Used for interactive login and as fallback if Microsoft
                        escalates to browser-only challenges (e.g. CF Turnstile).
  auth.py               Session caching: loads/saves cookies + MSAL access token
                        to session/token.json. Auto-refreshes via headless browser
                        when token is stale. Triggers interactive login on first run.
  protocol.py           Single source of truth for Copilot WebSocket wire protocol
                        constants (CHAT_WEBSOCKET_URL, SET_OPTIONS_FRAME, CONSENTS_FRAME).
                        Both driver.py and browser.py consume these.
  challenges.py         Proof-of-work challenge solvers (hashcash + copilot arithmetic).
  models.py             Shared data types: Conversation, ImageResponse, AbstractProvider.
  utils.py              Stateless helpers: HTTP status check, JSON frame draining, image encoding.

server/                 OpenAI-compatible HTTP layer on top of copilot/.
  api.py                FastAPI app with routes: GET /v1/models, POST /v1/chat/completions.
                        Contains upstream serialization lock (_upstream_lock), session
                        fingerprint cache (_session_cache), and rate limiting.
  config.py             Constants: MODEL_NAME, RATE_LIMIT_RPM, RATE_LIMIT_BURST.
  schemas.py            Pydantic models: ChatCompletionRequest, ChatMessage.
  prompt.py             Flattens OpenAI messages[] into a single Copilot prompt string.
  openai_format.py      Builds OpenAI-shaped response/chunk dicts for SSE streaming.
  ratelimit.py          Thread-safe token-bucket rate limiter.
```

## Key Design Decisions

**Single-account concurrency lock**: Microsoft Copilot's per-account chat WebSocket does not tolerate concurrent conversations. `_upstream_lock` in `server/api.py` serializes all upstream calls — concurrent HTTP requests queue and execute one at a time.

**Session fingerprint caching**: `get_messages_fingerprint()` in `server/api.py` hashes the conversation history (excluding the last message) to an MD5 fingerprint, mapped to a Copilot `conversation_id`. This enables multi-turn context continuity even when clients don't pass back `conversation_id` explicitly. When a fingerprint matches, only the last user message is sent to Copilot (not the full history), avoiding duplicate messages on the Copilot side.

**Two driver paths**: The primary path is `copilot/driver.py` (pure HTTP via `curl_cffi` impersonating Chrome, no browser needed). `copilot/browser.py` is the Playwright fallback, used for interactive login and when Microsoft requires browser-only challenges. Both share protocol constants from `copilot/protocol.py`.

**Auth model**: REST calls authenticate via cookies only (not Bearer token). The chat WebSocket carries identity via `?accessToken=` query param, which must be the MSAL `ChatAI.ReadWrite` scope token. Token is cached in `session/token.json` and refreshed via headless browser when stale (>90 min).

**WebSocket frame handling**: `curl_cffi`'s `WebSocket.recv()` loops on `CURLE_AGAIN` forever. `driver.py` implements custom `_recv_frame()` using raw `select()` + `recv_fragment()` with deadlines to avoid hanging on idle sockets.

**Model-to-mode mapping**: In `server/api.py`, model names like `copilot-reasoning`, `copilot-search`, `copilot-study` are mapped to Copilot protocol `mode` values. The base `copilot` and `copilot-smart` models map to `"smart"` mode.

## Protocol Maintenance

If Microsoft changes the Copilot chat protocol, use `tests/diagnostic.py` to recapture the wire format (output goes to `session/ws_capture.log`), then update `copilot/protocol.py`. Both `copilot/driver.py` and `copilot/browser.py` depend on those shared constants.
