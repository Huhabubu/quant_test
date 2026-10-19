# DouDou × Minecraft × Muse（阶段 0）

目标：让 **真实的 NeoForge 26.2 Minecraft 客户端**以玩家 DouDou 身份加入 mod 服务器，并逐步通过现有 Muse 账号控制。**不接 Muse 按量计费模型 API；不使用协议机器人；不改变服务端 mod。**

这里的代码是第一个、可单独验证的 **本地桥接组件**，并非完整 Minecraft bot。与量化实验代码隔离。

## 当前已实现 / 未实现

| 功能 | 状态 |
|---|---|
| 只监听 127.0.0.1 的 HTTP JSON 控制接口 | 已实现 |
| Bearer Token 鉴权（随机密钥，不入库） | 已实现 |
| 通过已存在的 tmux 会话向 HeadlessMC 控制台发送 `connect` 和 `msg` | 已实现，尚需在真实客户端验证 |
| 控制台命令白名单、聊天长度限制、换行注入拦截 | 已实现 |
| 确定客户端已经启动 / 已完成进服 | 未实现（status 明确返回 null） |
| 接收游戏内聊天、位置、血量 | 未实现；需要客户端侧事件/指标通道 |
| 移动、朝向、跟随、寻路 | 未实现；hmc-specifics README 未列出通用 `key` 命令，需自定义 mod 验证 |
| Muse 自定义连接器 | 未验证；需 Muse 实际环境配合 |
| 外网 HTTPS 接入 | 未配置；安全地完成本地测试后再做 |

## 环境

- Linux x86_64；Minecraft 26.2 + NeoForge 26.2.0.93；
- HeadlessMC 3.0.0-RC8；
- 游戏应使用 **JDK 25**，启动器也可用 JDK 25；
- 实例路径 `/root/.minecraft`，HeadlessMC 在 `/root/workspace/headlessmc`；
- mod 服务器 `用户私有的 MC 服务器地址（通过启动参数输入，不入库）`，客户端用户名 DouDou；
- 运行时需要 `tmux`，Python ≥ 3.11；Python 端无需新增第三方依赖。

**当前阻塞**：NeoForge 客户端尚未通过启动流程到达主菜单。若不能进入游戏，HTTP 返回 `accepted: true` 仅代表命令已经提交给 tmux，**不能当作游戏成功执行**。

## 本地验证方法

### 1. 先运行测试

仓库根目录：

```bash
python -m unittest -v test_doudou_bridge.py
```

### 2. 用 tmux 持有 HeadlessMC 控制台

在云服务器安装 `tmux` 后（路径按实际安装核对）：

```bash
tmux new-session -d -s doudou \
  'cd /root/workspace/headlessmc && exec ./jdk/jdk-25.0.4.1+1/bin/java -jar headlessmc.jar'
tmux attach -t doudou
```

在 tmux 内**手动**启动并确认 NeoForge 能进入游戏（可先尝试调整 early display 配置；如仍因 LWJGL 补丁卡住，可转 Xvfb + Mesa 软件渲染）。安装匹配 26.2 NeoForge 的 `hmc-specifics` JAR 后，先手动验证 `help`、`connect`、`msg` 命令是否确实可用。退出 tmux 显示时请用 `Ctrl-b d`，不要退出 Minecraft 进程。

hmc-specifics 官方发行版：
https://github.com/headlesshq/hmc-specifics/releases/tag/26.2-latest

### 3. 运行本地控制桥

另开一个 SSH 终端，在仓库根目录：

```bash
export DOUDOU_BRIDGE_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export MC_HOST='<服务器域名>'
export MC_PORT='<服务器端口>'
python -m minecraft_doudou.bridge --server-host "$MC_HOST" --server-port "$MC_PORT"
```

**令牌只能在本机/密码管理器保存，不要提交到 GitHub、Issue 或发给 Muse 的普通聊天。**

另一个使用相同令牌的本地 shell 中：

```bash
curl -sS -H "Authorization: Bearer $DOUDOU_BRIDGE_TOKEN" \
  http://127.0.0.1:8765/v1/status

curl -sS -X POST -H "Authorization: Bearer $DOUDOU_BRIDGE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"你好，我是 DouDou"}' \
  http://127.0.0.1:8765/v1/chat

curl -sS -X POST -H "Authorization: Bearer $DOUDOU_BRIDGE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}' http://127.0.0.1:8765/v1/connect
```

**预期**：
- 未授权访问 `/v1/status` 返回 401；
- tmux 存在时 `/v1/chat` 和 `/v1/connect` 返回 202、`verified:false`；
- 通过 **MC 游戏内聊天界面/日志**进一步确认是否发送成功；
- `game_loaded:null` 和 `server_connected:null` 表示当前还未完成状态采集。

## HTTP 接口（阶段 0）

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/health` | 本地桥服务存活（无需令牌，且只绑本机） |
| GET | `/v1/status` | tmux 会话是否存在；不声称 MC 已启动 |
| POST | `/v1/chat` | 请求体 `{"text":"..." }`，最多 200 字 |
| POST | `/v1/connect` | 请求体 `{}`，只连接预配置地址 |

所有 `/v1/*` 需要 `Authorization: Bearer <token>`；**没有通用 shell 命令执行入口**。

## Muse 的待办

参见仓库中的「Muse 配合」中文 Issue。需先确认 Muse 当前账号是否能创建私人 Custom Connector（自定义连接器）、调用自托管 HTTPS 服务、使用 Bearer Token，以及是否支持外部事件唤醒。**在验证前，不会开放本地控制桥的公网端口。**

最终方案是客户端 NeoForge mod 提供真实游戏状态与动作接口，Python 桥负责鉴权和行为约束，Muse 负责规划、聊天与记忆；未经授权的外网控制永不允许。

## 已知风险和下一步

1. 用 JDK 25 核对 Minecraft **实际子进程**的 Java 版本；继续定位 early-display/LWJGL 卡死。
2. 在真实 tmux/HeadlessMC 下验证命令是否确实到达 hmc-specifics；必要时替换 tmux 适配器。
3. 建立可靠的客户端状态与移动控制（需要 NeoForge 26.2 mod 和服务端 mod 列表）。
4. Muse 确认连接器能力后，再设计严格认证的 HTTPS 入口。
