# Windows Copilot API: 微软 Copilot 的免费 OpenAI 兼容接口

这是一个零门槛、零开销、100% 免费的本地 LLM 桥接服务。

它能够将您个人的微软 Copilot（包括免费版和 M365 高级版）网页端体验转化为标准的 **OpenAI 兼容 API**。

您无需任何 API Key、无需充值或订阅官方 API，即可在代码中、或在各类支持 OpenAI 协议的本地 AI 客户端（如 **OpenCode**, **OpenWebUI**, **NextChat**, **Dify** 等）中，直接享受微软 Copilot 强大的智能！

---

## 🌟 核心特性

- **🚀 智能会话保持（Session 一致性）**：
  引进了**历史消息指纹哈希（Fingerprint Caching）**机制。即使本地客户端（如 OpenCode）无法显式回传 `conversation_id`，API 也会通过分析多轮对话的上下文哈希，自动在后台重用已存在的 Copilot 会话。在 Copilot 侧只提交最后一条追问，保证上下文完美继承，**不再每发送一句就新增一个对话**！
- **🧠 深度思考与实时网络搜索**：
  底层直接打通微软 Copilot WebSocket 的多模式开关，提供四个高可用的特定模型，供您在客户端灵活选用：
  - `copilot-smart`（智能模式，根据任务自动权衡深度与速度）
  - `copilot-reasoning` / `copilot-thinking`（**深度思考**模式，通过复杂的逐步逻辑演绎、推理公理公式解决高难度问题）
  - `copilot-search`（**实时搜索**模式，包含最新的网页搜索和学术/引文来源标示）
  - `copilot-study`（学习研究模式，适合引导式测验与总结）
- **⚡ OpenAI 标准流式兼容**：
  完全支持 Event-Stream（SSE）逐字流式返回，响应迅速稳定。
- **🔌 零门槛免浏览器运行**：
  基于高效的 `curl_cffi` 模拟 Cloudflare 握手和直连 WebSocket 通信，不需要浏览器常驻前台。只需在首次启动时在弹出的浏览器中进行一次微软账户登录授权，即可在后台 headless 模式下自动持久化与刷新 Token，支持 Windows、macOS、Linux。
- **🐳 支持 Docker 部署**：
  提供 Docker 镜像构建与 `docker-compose` 快速部署。

---

## 📋 目录
- [为什么使用这个项目？](#为什么使用这个项目)
- [运行环境要求](#运行环境要求)
- [快速开始（2 分钟上手）](#快速开始2-分钟上手)
- [配置本地客户端（以 OpenCode 为例）](#配置本地客户端以-opencode-为例)
- [接口使用示例](#接口使用示例)
  - [获取可用模型列表](#1-获取可用模型列表)
  - [多轮对话请求（流式）](#2-多轮对话请求流式)
  - [Python OpenAI SDK 示例](#3-python-openai-sdk-示例)
- [使用 Docker 部署（可选）](#使用-docker-部署可选)
- [代理网络与排错（非常重要）](#代理网络与排错非常重要)
- [限制与说明](#限制与说明)
- [开源协议](#开源协议)

---

## 🤝 为什么使用这个项目？

1. **完全免费**：使用您个人的微软账户（即使是普通免费版），无额度耗尽担忧。
2. **免除上下文污染**：原装 API 通常在多轮聊天中粗暴地把所有的 `User/Assistant` 历史拼接为一条超长 prompt 重新发给微软，不仅导致网页端产生冗余会话，而且微软端无法建立正确的有状态上下文。本项目利用哈希映射完美维持单会话，体验更加自然。
3. **更低的网络风控**：通过将本地代理和 Playwright Headless 结合，实现 Token 跨域自动更新，能绕过在某些地区（如印度等）对匿名 Copilot 的地缘限制，只要您的账号处于支持区域即可正常调用。

---

## 💻 运行环境要求

- **Python 3.9+**
- 一个通用的**微软账户**（用于登录 Copilot 网页端）
- 良好的代理工具（处于中国大陆等不受支持地区的开发者请参见后文的[代理排错](#代理网络与排错非常重要)）

---

## ⚡ 快速开始（2 分钟上手）

### 1. 克隆代码并进入目录
```bash
git clone <本仓库地址>
cd Windows-Copilot-API
```

### 2. 创建并激活虚拟环境
在 **Windows (PowerShell)** 中运行：
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```
> *注：如遇到权限错误，可先执行 `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` 允许脚本运行。*

在 **macOS / Linux** 中运行：
```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. 安装依赖与 Playwright 环境
```bash
# 安装 Python 依赖
pip install -r requirements.txt

# 安装 Playwright 的 Chromium 浏览器内核（仅在首次运行时需要）
playwright install chromium
```

### 4. 授权登录您的微软账户
运行以下命令以进行首次登录：
```bash
python -m copilot login
```
系统会拉起一个可见的 Chromium 窗口。**在该窗口中手动登录您的微软 Copilot 账户**。
登录成功并跳转至 Copilot 聊天主页后，**程序会自动捕获 Token 窗口并自动关闭浏览器**（无需您手动关闭或按回车）。您的登录态以加密安全形式保存在本地 `session/` 目录中，永不上报且 git-ignore 隔离。

### 5. 启动 API 服务
使用代理运行 API 服务器（监听在 `http://127.0.0.1:8000`）：
```powershell
# Windows PowerShell 设置临时代理并启动
$env:HTTP_PROXY="http://127.0.0.1:7890"
$env:HTTPS_PROXY="http://127.0.0.1:7890"
python app.py
```
若您处于直连无需代理的环境下，直接运行 `python app.py` 即可。

---

## 🔌 配置本地客户端（以 OpenCode 为例）

要将 OpenCode 客户端接入本项目：
直接修改或创建您的 OpenCode 配置文件 `C:\Users\YourUsername\.config\opencode\opencode.jsonc`（或 `.json`），在 `"provider"` 中注册本服务：

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "model": "windows-copilot/copilot", // 默认启动模型
  "provider": {
    "windows-copilot": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Windows Copilot API (local)",
      "options": {
        "baseURL": "http://127.0.0.1:8000/v1"
      },
      "models": {
        "copilot": {
          "name": "Copilot (Default)"
        },
        "copilot-smart": {
          "name": "Copilot (Smart)"
        },
        "copilot-reasoning": {
          "name": "Copilot (Deep Thinking)"
        },
        "copilot-thinking": {
          "name": "Copilot (Thinking)"
        },
        "copilot-search": {
          "name": "Copilot (Search)"
        },
        "copilot-study": {
          "name": "Copilot (Study)"
        }
      }
    }
  }
}
```
保存配置后，重启 OpenCode 客户端或在输入框中运行 `/models`，即可看到并在下拉菜单中自由选用这些不同模式的 Copilot 模型！

---

## 📡 接口使用示例

本项目全面兼容标准 OpenAI 端点。

### 1. 获取可用模型列表
```bash
curl http://localhost:8000/v1/models
```
**返回结果：**
```json
{
  "object": "list",
  "data": [
    {"id": "copilot", "object": "model", "created": 0, "owned_by": "microsoft"},
    {"id": "copilot-smart", "object": "model", "created": 0, "owned_by": "microsoft"},
    {"id": "copilot-reasoning", "object": "model", "created": 0, "owned_by": "microsoft"},
    {"id": "copilot-thinking", "object": "model", "created": 0, "owned_by": "microsoft"},
    {"id": "copilot-search", "object": "model", "created": 0, "owned_by": "microsoft"},
    {"id": "copilot-study", "object": "model", "created": 0, "owned_by": "microsoft"}
  ]
}
```

### 2. 多轮对话请求（流式）
发送第一句：
```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "copilot-reasoning",
    "messages": [
      {"role": "user", "content": "你好，我是小明。记下我的名字。"}
    ],
    "stream": true
  }'
```
发送第二句时（直接携带完整的历史列表，本服务将在后台指纹匹配并自动沿用 `conversation_id`，但在向微软发送时仅发最后一句话，防止网页端产生垃圾消息和重叠会话）：
```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "copilot-reasoning",
    "messages": [
      {"role": "user", "content": "你好，我是小明。记下我的名字。"},
      {"role": "assistant", "content": "你好小明！我已经记住了。"},
      {"role": "user", "content": "刚才我说我叫什么？"}
    ],
    "stream": true
  }'
```

### 3. Python OpenAI SDK 示例
```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="unused")

# 1. 正常提问 (智能模式)
response = client.chat.completions.create(
    model="copilot-smart",
    messages=[{"role": "user", "content": "写一首赞美春天的简短十四行诗。"}],
    stream=False
)
print(response.choices[0].message.content)

# 2. 深度思考
response_reasoning = client.chat.completions.create(
    model="copilot-reasoning",
    messages=[{"role": "user", "content": "请详尽推理为什么 1+2 等于 3。"}],
    stream=True
)
for chunk in response_reasoning:
    content = chunk.choices[0].delta.content
    if content:
        print(content, end="", flush=True)
```

---

## 🐳 使用 Docker 部署（可选）

如果您希望以容器化方式运行服务，请先在宿主机上完成 `python -m copilot login`。由于容器内部通常无法运行带界面的浏览器来完成首次授权，因此需要先在宿主机上生成 `session/` 文件：

```bash
# 启动容器，挂载本地 session/ 目录
docker compose up --build
```
容器会在启动后通过本地挂载的 session 目录，在容器内部静默完成所有无头模式的 Token 周期性刷新。

---

## 🌍 代理网络与排错（非常重要）

如果您在调用接口时遇到 `net::ERR_CONNECTION_CLOSED` 或 `502 Bad Gateway` 等报错，通常是由代理风控或冲突导致的。请注意以下几点：
1. **全局代理设置**：
   如果您使用的代理工具是 **Clash** 且启用了**规则模式**（Rules），Playwright 所派生的 `chrome.exe` 可能被特定规则强行直连（DIRECT）阻断，导致连接 `copilot.microsoft.com` 失败。此时，请尝试将 Clash 切换为 **全局模式（Global）** 运行。
2. **进程拦截避让**：
   在后台运行 Python 脚本或容器时，确保代理的本地监听端口正确（通常为 `http://127.0.0.1:7890`），并在启动 API 服务时正确输入 `HTTP_PROXY` / `HTTPS_PROXY` 环境变量。

---

## ⚠️ 限制与说明

- **并发限制**：由于微软 Copilot 单个账号不支持并发对话处理（同时发送多条请求会产生冲突或死锁），本项目服务端在内部加了**排队锁（Thread upstream lock）**。如果多个客户端同时请求本地服务，请求会进行排队串行处理。本项目极其适合个人日常开发、辅助编码与个人助理使用。
- **免责声明**：本项目为开源学习交流项目，不隶属于微软。请负责任地使用此工具并遵守相关协议。

---

## 📄 开源协议

本项目基于 [MIT License](LICENSE) 协议发布。
