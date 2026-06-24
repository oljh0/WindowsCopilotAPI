# 示例说明文档 (Examples Guide)

本目录包含两套可以直接运行的示例代码，分别演示了直接使用 Python 客户端类以及通过本地 OpenAI 兼容服务器与 Windows Copilot API 进行交互。

我们最近针对本地客户端遇到了 **“每发送一句消息就会在网页端新增一个对话”** 的会话泄露问题，并在 API 层面通过 **“历史消息指纹哈希（Fingerprint Caching）”** 进行了修复。本篇文档将介绍该问题、修复原理、新模式（深度思考/网络搜索）在示例中的体现以及运行结果。

---

## 📂 示例文件结构

### 1. 直连交互（无需本地服务器，基于 `CopilotClient`）
在首次运行时，直连方式会自动在本地拉起可见浏览器供您进行一次微软账号登录。

| 脚本文件 | 演示功能说明 |
| --- | --- |
| [01_direct_chat.py](01_direct_chat.py) | 最简单的单轮交互（One-shot Chat） |
| [02_direct_conversation.py](02_direct_conversation.py) | 多轮会话演示 —— 需通过 `conversation_id` 显式进行上下文继承 |
| [03_direct_stream.py](03_direct_stream.py) | 流式结果（SSE）逐字输出演示 |

### 2. 通过本地 OpenAI 兼容服务器（需先运行 `python app.py`）
该方式完全屏蔽了底层逻辑，提供了标准的 OpenAI API 端点。您可以使用任何支持 OpenAI 格式的客户端（如 `openai` 官方 Python SDK）进行免参数转换的接入。

| 脚本文件 | 演示功能说明 |
| --- | --- |
| [04_server_http.py](04_server_http.py) | 基于 `requests` 的原生 HTTP 多轮会话 |
| [05_server_stream.py](05_server_stream.py) | 基于 Server-Sent Events 的流式端点调用 |
| [06_server_openai_sdk.py](06_server_openai_sdk.py) | 使用官方 `openai` 库调用本地 API 服务的最佳实践 |

---

## 🛠️ 问题、修复步骤与设计

### 问题背景
由于大部分标准 OpenAI 客户端（例如 OpenCode, OpenWebUI 等）不会主动回传有状态的会话参数 `conversation_id`，而是在用户继续提问时直接发送完整的 `messages` 历史列表。
原先的 API 桥接会将所有的 messages 拼接成一大段历史 prompt 发送给微软，这会导致 Copilot 的后端因接收到没有会话 ID 的全新请求而**在网页端不断创建全新的空对话**。

### 修复步骤 (指纹缓存设计)
我们在 `/v1/chat/completions` API 内部建立了一套基于**多轮对话哈希指纹**的 session 保持缓存：
1. **哈希提取**：提取传入 `messages` 列表的前置对话部分（即 `messages[:-1]`，排除最新问题），计算序列化 MD5 得到会话指纹键。
2. **会话复用**：如果哈希键在服务端的 `_session_cache` 字典中命中，说明是继续前面的会话。API 将自动提取绑定的 `conversation_id`，且向微软发送时**仅仅发送最新一轮的用户消息**，完美继承上下文且杜绝网页端新对话的滋生。
3. **动态录入**：在流式或非流式对话输出结束时，API 获取微软返回的真实会话 ID，并将最新的 `messages + assistant_reply` 压入缓存，为下一轮匹配做准备。

---

## 🧠 使用新增的自定义模型模式

得益于新版本对底层 WebSocket 通信帧的打通，您在调用服务器端的示例时，可以直接通过指定 `model` 参数来切换不同的 Copilot 处理模式：
* `"copilot-smart"`：默认智能模式。
* `"copilot-reasoning"` 或 `"copilot-thinking"`：**深度思考**模式（更适合需要逻辑、公理、复杂证明的科学与算法提问）。
* `"copilot-search"`：**实时搜索**模式（当您需要获取今天的实时新闻、股票或天气时选用，自带文献引文）。
* `"copilot-study"`：研究学习模式。

---

## 📈 运行结果验证与证据

在运行测试和示例脚本后，我们成功验证了这些修复和新特性的工作情况：

### 1. 多轮会话一致性结果
运行测试脚本发送第一句“我是小明”，紧接着发送“我刚才说我叫什么”：
* **服务端控制台输出**：
  ```text
  [SessionCache] Hit. Reusing conversation_id: HAv6PSgknuXEuLjgdzTy2
  ```
* **AI 回答**：
  ```text
  你的名字是：小明。你的职业是：程序员。
  ```
* **结论**：本地客户端无需手动管理会话，即可实现完美的 Session 保持。

### 2. 深度思考模式结果
选用 `copilot-reasoning` 提问 “如何公理化证明 1+2=3”：
* **AI 回答**：
  ```text
  结论：在常见的公理化自然数体系下，1+2=3。
  ### 公理化设定（Peano 公理）
  - 设 0 和后继算子 S(·)。
  - 1 := S(0), 2 := S(S(0)), 3 := S(S(S(0)))
  ### 逐步计算：
  - 1 + 2 = 1 + S(1) = S(1 + 1)
  - 1 + 1 = 1 + S(0) = S(1 + 0) = S(1)
  - 所以 1 + 2 = S(S(1)) = S(2) = 3。
  ```

### 3. 实时网络搜索结果
选用 `copilot-search` 提问最新微软股价：
* **AI 回答**：
  ```text
  微软今天收盘股价为 373.94 美元，上涨 6.60 美元，涨幅约 1.80%。
  - 市值：约 2.78 万亿美元
  - 开盘价：372.38 美元
  ```
* **结论**：实时搜索模式能够无障碍调用搜索引擎并自动提炼出包含最新动态的摘要。
