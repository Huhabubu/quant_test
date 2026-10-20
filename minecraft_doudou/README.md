# DouDou × Minecraft × Muse｜同 VM 控制桥

目标：现有 Muse 个人账号控制真正的 Minecraft 26.2 / NeoForge 客户端：聊天、移动，随后再做状态读取和自主陪玩。无需单独计费的 Muse Model API、不使用协议机器人、不改变服务端 mod。

## 2026-10-20 09:35 最新环境（Muse 回复；开发端尚未远程实测）

| 项目 | 现状 |
|---|---|
| 主机 | Muse 与 HeadlessMC 在同一台云 VM，HOME=/home/hatch |
| HeadlessMC | 10-20 03:50 runtime 重启后已退出，**目前没有 Java 进程** |
| 控制台 | 使用持久 PTY（模拟终端）的 stdin/stdout；没有 tmux |
| 游戏版本 | Minecraft 26.2 / NeoForge **26.2.0.88** / hmc-specifics **3.0.0** |
| 游戏准备 | 曾进入主菜单；27 个 mod 已齐，Alex's Mobs 已安装；待真正进服 |
| Muse | 私人自定义连接器可用；无外部 webhook 实时推送，支持分钟级轮询 |
| 网络 | **同 VM 优先使用 127.0.0.1**；无需公网 HTTPS 网关 |

## 为什么要让 Python 从一开始持有控制台？

正在运行的 Java 进程，其 stdin（标准输入）可能属于另一个交互终端。另起 Python 程序无法安全、可靠地夺取它的 stdin。

因此新增 **stdio_bridge.py**：由 Python 的 subprocess.Popen 启动**原来的 Java 命令及游戏参数**，持续写 stdin、读取 stdout（标准输出），并对本机开放受 Token 保护的 HTTP 接口。无需 tmux。它会改变启动命令的外层父进程，需要在**下一次**启动时使用；无法接管之前已经独立启动的 Java 进程。

**已新增 `--pty` 模式**，保留 Muse 已成功使用的交互式伪终端及 HeadlessMC 默认 JLine 行为；本次无需强制添加 `-Dhmc.jline.enabled=false`。旧管道模式仅作为兼容选项。

**下次启动前请先确认没有 HeadlessMC / MC 正在运行，避免重复实例。** 如果此前已经成功的原始启动命令有其他参数，请保留那些参数；不要重新下载或更新现有 NeoForge 26.2.0.88。

## HTTP 接口

全部监听在 **127.0.0.1:8765**，其中 /v1/* 需要 Authorization: Bearer Token。Token 应由本机生成并仅保存在 Muse 安全凭据系统/私密环境，不能复制进 GitHub、Issue 或普通聊天。

| 方法 | 路径 | 功能 |
|---|---|---|
| GET | /health | 本地 HTTP 服务存活 |
| GET | /v1/status | 控制台进程是否运行；不保证游戏到主菜单或进服 |
| GET | /v1/output | 从 Java stdout 缓存读取最近最多 40 行，受 Bearer 保护 |
| POST | /v1/launch | 请求体 {}：每个桥进程只允许一次，提交 `launch neoforge 26.2 --offline -lwjgl -eula -j "-Xmx5G"` |
| POST | /v1/connect | 请求体 {}：连接预配置的 MC 服务器 |
| POST | /v1/chat | 请求体 {"text":"你好"}，最长 200 字符 |
| POST | /v1/move | {"direction":"forward","duration_ms":300} |

移动只允许 forward/back/left/right，映射默认 W/S/A/D。单次按键 50–1000ms，自动释放，使用 hmc-specifics 的 key --duration 命令；玩家修改过按键绑定时，需先在游戏控制台检查实际键名。官方源码：https://github.com/headlesshq/hmc-specifics/blob/main/hmc-specifics-api/src/main/java/me/earth/headlessmc/mc/commands/KeyCommand.java

**任何写入返回 accepted:true, verified:false 只代表控制台收到了文本提交；实际游戏是否执行需要额外确认。** /v1/output 是最近的控制台输出而非可靠的游戏状态服务。

## 验证步骤

先在仓库根目录运行 Python 单元测试（只用模拟 Java 子进程，不会启动 MC）：

~~~bash
python3 -m unittest -v test_doudou_bridge.py test_stdio_bridge.py test_pty_bridge.py
~~~

下一次由 Muse 在云 VM 里启动时，**下面只是启动形式示例**。JAVA25 必须指向 Muse 已经验证可用的 Java 25 可执行文件；请保留此前实际进入主菜单时成功的 HeadlessMC 命令参数，特别是 NeoForge .88 对应实例。启动期间不要另起第二份 HeadlessMC：

~~~bash
# 在已检出的 quant_test 仓库根目录执行
export JAVA25="/absolute/path/to/working/java25/bin/java"
export DOUDOU_BRIDGE_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export MC_HOST="<私有服务器域名>"
export MC_PORT="<私有服务器端口>"

python3 -m minecraft_doudou.stdio_bridge \
  --pty --cwd "$HOME/workspace/headlessmc" \
  --server-host "$MC_HOST" --server-port "$MC_PORT" -- \
  "$JAVA25" -jar "$HOME/workspace/headlessmc/headlessmc.jar"
~~~

本 Python 进程通过 PTY 持有 HeadlessMC stdin/stdout。它关闭时，会停止**由它自己启动的 Java 进程**。不要在正常联机时随意停止这个 Python 父进程。

**第二阶段：** 等 `/v1/output` 确认 HeadlessMC 交互命令台已启动，再用 Bearer 鉴权发送一次 `POST /v1/launch`、请求体 `{}`。该接口发送以下**固定**指令：

~~~text
launch neoforge 26.2 --offline -lwjgl -eula -j "-Xmx5G"
~~~

重复请求将返回 400。请勿同时手工再输入一次 launch。Muse 实测到达主菜单需要数分钟，期间观察日志即可。

在同一 VM、**同一 Token 已安全注入的另一个会话**中检查：

~~~bash
curl -sS -H "Authorization: Bearer $DOUDOU_BRIDGE_TOKEN" \
  http://127.0.0.1:8765/v1/status

curl -sS -H "Authorization: Bearer $DOUDOU_BRIDGE_TOKEN" \
  http://127.0.0.1:8765/v1/output

curl -sS -X POST -H "Authorization: Bearer $DOUDOU_BRIDGE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"你好，我是 DouDou"}' \
  http://127.0.0.1:8765/v1/chat
~~~

成功进入服务器后，确保角色周围安全，再验证移动：

~~~bash
curl -sS -X POST -H "Authorization: Bearer $DOUDOU_BRIDGE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"direction":"forward","duration_ms":200}' \
  http://127.0.0.1:8765/v1/move
~~~

/v1/output 可能包含玩家聊天、其他用户信息或凭据，上传 Issue 前务必脱敏。出现 stdout 读取失败或 Java 提前退出时，把**脱敏后的错误上下文**回复至 Issue。

## Muse 下一步协作

详见 [Issue #2](https://github.com/Huhabubu/quant_test/issues/2)。

Muse 已于 09:35 确认成功启动的两阶段命令和运行条件：JDK25、持久 PTY、NeoForge 26.2.0.88、游戏堆内存 5G。云 VM 的进程在部署重启后**可能无预警消失**；Muse 可以负责保存定时任务并检查，但必须避免重叠启动和重复联机。桥本身不保证跨部署恢复，也不自动创建 cron。

随后验证 status、output、chat、connect、move 的实机闭环。当前不开发 HTTPS 网关，不使用 tmux。旧 TmuxConsole 仅留作其他环境的兼容实现。

Muse 的自主玩法按“分钟级定时检查 + 用户主动召唤”推进，实时动作由客户端本地完成。没有通用外部 webhook 即时唤醒的能力证明。

## 离线昵称需要单独核实

Muse 报告 `--offline` 当前用户名是 `Offline`，而用户目标为 `DouDou`。下次联调需先核实 HeadlessMC 的离线账号创建/选择方法，在实际登录时使用 DouDou。不要把凭据放进 GitHub，也不要通过改服务端 mod 来处理昵称。
