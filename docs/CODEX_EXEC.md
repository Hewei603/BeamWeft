# Codex exec 订阅接入

BeamWeft 2.0 在首页 Agent 配置提供两种接入方式：

- **API Key**：沿用原有 OpenAI、Anthropic 和其他兼容服务商配置。
- **Codex exec（ChatGPT 订阅）**：调用后端电脑安装的官方 Codex CLI，复用 CLI 的 ChatGPT 登录，不需要向 BeamWeft 提供模型 API Key。

## 开始使用

1. 在运行 BeamWeft 后端的电脑安装官方 Codex CLI。Windows 可以使用 npm 安装：npm install -g @openai/codex。也可指定已安装的 codex.exe。
2. 在同一操作系统用户的终端执行 codex login，选择 ChatGPT 登录。用 codex login status 确认显示 ChatGPT。
3. 打开 BeamWeft 首页的 **Agent 配置**，或在工程对话中点 **模型设置**，将接入方式选为 **Codex exec（ChatGPT 订阅）**。
4. 在模型栏选用建议模型，或输入账户可用的模型 ID，并在旁边选择 **推理强度**。Codex exec 默认使用 `gpt-6.1-sol` 和 `medium`。点击 **刷新登录状态**，再点 **测试连接**，用少量订阅额度验证所选配置。
5. 点击“保存并应用”。后续 Agent、实验计划、图片分析、需求提取、报告和对话摘要使用 Codex exec，无需重启。升级代码后的旧后端进程仍需重启一次。

高级设置可调整 Codex 可执行文件路径和每轮等待上限（30–1800 秒，默认 300 秒）。程序优先发现 Windows 原生 codex.exe，也支持通过 Node.js 启动官方 npm 包装脚本。不同模型和账号开放的推理档位以实际调用为准。

使用 **OpenAI / Compatible API** 的 Agent 工具对话时，选择具体档位会将它随 Responses 请求发送给服务，选择 **自动** 则由服务决定。历史任务中记录的模型和档位可以从 **任务结果** 查回，修改配置不会为旧记录补写一个推测值。[接口传输验收](../output/acceptance/agent-model-effort/README.md)

## 订阅与配置

Codex exec 使用当前登录账户的 Codex 订阅额度，仍受套餐限额约束。BeamWeft 记录 CLI 返回的 token 使用量，但不套用 API 单价，也不显示“免费无限使用”。选择这个方式时，子进程不继承 OPENAI_API_KEY 或 CODEX_API_KEY，并要求 ChatGPT 登录；CLI 使用 API Key 登录时会显示明确提示。

BeamWeft 不读取或保存 CLI 的 OAuth token，也不修改用户的全局 Codex 配置。登录、续期和账户切换交给 Codex CLI。后端在 Docker、远程服务器或其他系统用户下运行时，需要在那个运行环境安装 CLI 并完成登录；浏览器所在电脑的登录不会自动传到服务器。

首页保存的配置位于原有 AGENT_SETTINGS_FILE（默认上传目录中的 .agent-settings.json）。切换接入方式会保留各 API 服务商已保存的密钥、模型和接口地址。

可通过环境变量配置：

    AI_PROVIDER=codex_exec
    CODEX_EXEC_MODEL=gpt-6.1-sol
    CODEX_EXEC_PATH=
    CODEX_EXEC_TIMEOUT_SECONDS=300
    CODEX_EXEC_REASONING_EFFORT=medium

## Agent 与物理工具如何衔接

每轮 codex exec 在独立临时目录接收任务、历史上下文、真实工具结果和必要图片，通过 --output-schema 返回结构化动作。BeamWeft 校验工具名与参数，执行原有元件库、知识检索、ABCD 数值仿真、扫描和工程保存工具，再把实际结果交给下一轮。最终结论引用数据库记录的真实工具调用ID。

Codex 的临时调用使用只读沙箱，关闭本适配器不需要的 shell、补丁与多 Agent 功能，不加载用户 config.toml 或项目 AGENTS.md。文件修改和实验数据保存仍走 BeamWeft 原有业务工具。CLI 退出成功、收到 turn.completed 且取得有效结构化结果后，才接收该轮输出。临时图片和输出随调用结束清理。

如果 CLI 已经返回，但这一步失败，任务回执会列出进程退出码，以及工作台是否收到了错误事件和完成事件。退出码为 0 的进程也可能留下不完整的任务。诊断在有相应信号时提示连接、登录、额度或模型问题。已经完成的计算可以从同一轮的工具记录继续查阅。[公开任务接口回归](../backend/tests/test_codex_exec.py)

CLI 缺失、登录失效、限流、模型不可用和超时均显示错误；不会悄悄降级到 Mock，也不会自动改走 API 计费。工作台已完成的工具结果仍可用于继续任务。

## 验证

自动测试覆盖配置持久化、API profile保留、图片传入、实际工具执行、结构化响应、超时及取消、登录与额度错误、未知工具拒绝以及订阅用量计费标记。

真实登录验收在临时数据库运行，不修改已有实验案例：

    cd backend
    .venv\Scripts\python.exe scripts\validate_codex_exec_live.py

它会使用当前账户的订阅额度，验证连接、图片输入、Agent调用实际物理内核、保存证据与报告生成，证据输出到 output/validation/codex-exec-live.json。

官方参考：[Codex非交互模式](https://learn.chatgpt.com/docs/non-interactive-mode)、[Codex登录方式](https://learn.chatgpt.com/docs/auth)。
