# DouDou × Minecraft × Muse（阶段 0：控制桥）

目标：把**真实的 Minecraft 26.2 + NeoForge 客户端**交给现有 Muse 个人账号控制，包括聊天、移动，后续加入游戏事件与跟随。**不使用额外计费的模型 API、不使用协议机器人、不修改服务端 mod。**

此目录仅包含 Python 最小桥接组件，和仓库中的量化实验隔离。

## 当前事实与报告来源（2026-10-20）

| 项目 | 当前情况 |
|---|---|
| 真实客户端 | **据 Muse 2026-10-20 00:25 反馈**，已经进入主菜单；尚未经本仓库自动验证 |
| 已运行构建 | Minecraft 26.2 / **NeoForge 26.2.0.88** / hmc-specifics **3.0.0** |
| Muse 已验证的命令 | `connect`、`msg`、`key`、`gui` |
| 当前进服阻塞 | 尚缺 Alex's Mobs 的匹配客户端 mod：`alexsmobs-2.1.9-neoforge+26.2.jar` |
| Muse 控制方式 | 个人 Custom Connector 可用；自托管 HTTPS + Bearer 待真实连接测试 |
| 后台唤醒能力 | **无外部实时 webhook 推送**；支持分钟级定时/条件轮询，但存在用户设置的主动消息时间窗口 09:00–21:30 |

版本、进度、路径均来自 Muse 的反馈，**不可将其视为本仓库已经在云端实测成功**。

## 首先确定机器边界

Muse 报告真客户端位于 **Muse 云 VM** 的 `~/workspace/headlessmc/`；旧版文档以用户另一台机器的 `/root/workspace/headlessmc/` 为例，已不再作为部署前提。

**方案 A（优先）**：若 Muse 执行代码的云 VM 与 HeadlessMC **是同一台、同一文件/进程命名空间**，可直接用 `tmux` 命令或本机 `http://127.0.0.1:8765` 控制客户端。**无需额外公网 HTTPS 网关**。

**方案 B（仅当 A 不成立）**：Muse 执行连接器的云 VM 与 HeadlessMC 不共享本地进程，则在客户端所在 VM 运行本地桥，再由 HTTPS 反向代理/私有网络出口提供严格鉴权的入口；凭据仅入 Muse Secure Vault，**8765 仍只能监听 loopback**。

请 Muse 在实际运行客户端的 VM 上执行**不涉及账号凭据**的诊断：

```bash
pwd
printf 'HOME=%s\n' "$HOME"
test -f "$HOME/workspace/headlessmc/headlessmc.jar" && echo 'HMC_JAR_FOUND'
ps -eo pid,ppid,cmd | grep -E '[h]eadlessmc|[n]eoforge|[j]ava'
tmux list-sessions
```

如果 `tmux` 未安装，或现有 HeadlessMC **没有运行在 tmux 会话中**，当前 `TmuxConsole` 控制方式无法工作。**不要再启动第二个 Minecraft 客户端**；先确认原实例的控制台输入方式，必要时改适配器。只有已经存在、实际连接着 HeadlessMC 控制台的 tmux 会话才能直接使用下方桥接代码。

## 已实现接口

`bridge.py` 仅使用 Python 标准库，需要 Python 3.11+。每个 `/v1/*` 请求都需要随机 Bearer Token（HTTP 请求授权密钥）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 仅本机可访问的服务存活检查 |
| GET | `/v1/status` | 当前 tmux 会话是否存在；**不能**确认游戏已经加载或连服 |
| POST | `/v1/chat` | `{"text":"你好"}`：提交 `msg` |
| POST | `/v1/connect` | `{}`：提交 `connect <预配置域名> <预配置端口>` |
| POST | `/v1/move` | `{"direction":"forward","duration_ms":300}`：提交 `key w --duration 300` |

移动方向只允许 `forward/back/left/right`，对应默认 W/S/A/D；单次按键持续 **50–1000ms**，由 hmc-specifics 自动释放。若用户改过按键映射，需先在客户端控制台用 `key` 检查实际名称。

**重要**：HTTP 202 和 `{"accepted":true,"verified":false}` 仅说明命令已经成功写入 tmux；它不表示 Minecraft 已执行。无法证明游戏主菜单、连服状态、聊天送达或移动完成。目前也尚不能读游戏内聊天和位置。

官方 KeyCommand 源码：https://github.com/headlesshq/hmc-specifics/blob/main/hmc-specifics-api/src/main/java/me/earth/headlessmc/mc/commands/KeyCommand.java

## 本地验证（客户端所在的机器）

运行仓库根目录的聚焦测试：

```bash
python3 -m unittest -v test_doudou_bridge.py
```

只在确认 HeadlessMC 控制台**已经**位于 `tmux` 会话（如 `doudou`）后，启动桥接：

```bash
# 在仓库根目录执行；服务器配置通过本机参数传入，不提交到 GitHub
export DOUDOU_BRIDGE_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
python3 -m minecraft_doudou.bridge \
  --tmux-session doudou \
  --server-host '<你的MC服务器域名>' --server-port <你的MC服务器端口>
```

另外打开**同一台 VM**的本机终端，使用同一个环境令牌检查：

```bash
curl -sS -H "Authorization: Bearer $DOUDOU_BRIDGE_TOKEN" \
  http://127.0.0.1:8765/v1/status

curl -sS -X POST -H "Authorization: Bearer $DOUDOU_BRIDGE_TOKEN" \
  -H "Content-Type: application/json" -d '{"text":"你好，我是 DouDou"}' \
  http://127.0.0.1:8765/v1/chat

curl -sS -X POST -H "Authorization: Bearer $DOUDOU_BRIDGE_TOKEN" \
  -H "Content-Type: application/json" -d '{"direction":"forward","duration_ms":300}' \
  http://127.0.0.1:8765/v1/move
```

上述输出须配合客户端游戏日志/实际玩家位置验证。**不要把 Token、Cookie、Muse 登录凭据贴到 GitHub 或普通聊天。**

## Muse 协作与下一步

协作事项记录在 [Issue #2](https://github.com/Huhabubu/quant_test/issues/2)。Muse 优先确认：云 VM 的客户端和它执行自建 helper 的位置是否共享文件/进程；HeadlessMC 是否附着在 tmux；可否通过本地命令直接成功发送一条游戏消息。

如果本机直连成功，**先不开发 HTTPS 网关**；若不成功且隔离原因明确，再做 HTTPS + Secure Vault。

最终交互按**定时检查 + 用户在 Muse 端下令时执行**设计，不能承诺 Minecraft 游戏内实时喊话一定会触发 Muse。后续补充状态采集、受控连续行动、真实 NeoForge mod 握手和联调证据。
