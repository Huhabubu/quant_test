# DouDou × Minecraft × Muse｜2026-10-20 成功联调经验固化

**阶段：Draft PR #3，修正下一次启动。** Muse 于 2026-10-20 13:23 报告，用手动 PTY 会话已经成功进入真实 Minecraft 服务器，英文聊天和移动已验证。新桥的自动准备与字符编码改动**仍需云 VM 实机验证**。如果现有 DouDou 还在线，不要启动第二份客户端或替换正在运行的进程。

## 已确认的环境

- Minecraft 26.2；NeoForge **26.2.0.88**；JDK 25；hmc-specifics 26.2-3.0.0。
- HeadlessMC 位于 Muse VM 的 /home/hatch/workspace/headlessmc/（实际路径以 Muse 现场核对为准）。
- 游戏预装路径：游戏根目录 gamedir，实例 gamedir/neoforge-26.2。
- 第二阶段成功的游戏命令：launch neoforge 26.2 --offline -lwjgl -eula -j "-Xmx5G"。
- 7.7GB VM 上堆内存 **5G** 稳定，6G 曾被 OOM killer 杀。
- 服务器 mod 注册表已验证成功；World Bosses + GeckoLib 已加入；JourneyMap 禁用后，Muse 报告进服稳定。
- VM 可能无预警重启并回滚启动器配置。**不要依赖启动器配置持久化，也不要启用尚未经验证的自动重启看门狗。**

## 本轮修正了哪些失败根因？

1. **启动到了错误的 Minecraft 目录**：此前只配置了 --cwd，HeadlessMC 落到 /root/.minecraft，选了 .93，重新下载 481MB 且缺少 mod。现在强制 --mcdir 参数，在启动 Java 前验证现有模组实例，并向 launcher JVM 仅注入当前的 hmc.files.mc 属性（不再同时写旧版 hmc.mcdir，避免 Muse 实测的双配置冲突警告）。/v1/prepare 也使用 config set hmc.files.mc。
2. **账号变成 Offline**：保留上次账号记录并不代表当前 HeadlessMC 会话已选择 DouDou。/v1/prepare 发送 account -p offline select DouDou，使用**已有账号**，不重新生成 UUID。**如果该命令报错，不要继续 launch**。Muse 已验证 account -p offline login 的手动交互流程；若 select 未能恢复账号，请在 Issue 留下脱敏失败日志。
3. **模组缺失 / JourneyMap 崩溃**：预检检查现有 NeoForge 目录、足量 JAR、hmc-specifics、World Bosses、GeckoLib、fml.toml 的 earlyWindowControl=false，发现启用 JourneyMap 就拒绝启动。不删除或移动用户的文件。
4. **中文聊天发送乱码**：Java launcher 的输入、输出、文件编码增加 UTF-8 属性；PTY 的非 UTF-8 locale 自动修正，添加模拟中文往返测试。Muse 于 16:05 在手动会话使用 LC_ALL=C.UTF-8 时实测中文正常；**在桥下仍待复验**。
5. **反复慢启动**：先排除错误目录、重复下载和崩溃造成的重复冷启动。原正常 NeoForge 启动仍约需数分钟，还未提供逐阶段 CPU/IO 耗时数据。

## 测试（不会启动真实 MC）

在 quant_test 仓库的 feature/doudou-minecraft-bridge 分支运行：

~~~bash
python3 -m unittest -v test_doudou_bridge.py test_stdio_bridge.py test_pty_bridge.py test_doudou_preflight.py test_doudou_events.py test_doudou_account.py
~~~

## 下一次从零启动（仅在现有 DouDou 下线且无遗留实例时）

**注意：** 该命令只启动 HeadlessMC 控制台，**不直接 launch 游戏**；游戏指令在后面单独发。

~~~bash
# 在 quant_test 仓库根目录，MC 的实际磁盘路径应由 Muse 先核实
export HMC_HOME="/home/hatch/workspace/headlessmc"
export DOUDOU_BRIDGE_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export MC_HOST="<私有服务器域名>"
export MC_PORT="<私有服务器端口>"

python3 -m minecraft_doudou.stdio_bridge \
  --pty --cwd "$HMC_HOME" --mcdir "$HMC_HOME/gamedir" \
  --account-state-file "/root/.local/share/headlessmc/.auth/last/.accounts.json" \
  --owner-name "Huhabubu" \
  --server-host "$MC_HOST" --server-port "$MC_PORT" -- \
  "$HMC_HOME/jdk/jdk-25.0.4.1+1/bin/java" \
  -jar "$HMC_HOME/headlessmc.jar"
~~~

运行前预检必须通过：gamedir/neoforge-26.2/mods 存在且至少 20 个 JAR，包含 hmc-specifics、World Bosses 和 GeckoLib；fml.toml 有 earlyWindowControl=false；JourneyMap JAR 不在启用的 mods 目录。**如果现场的版本/目录结构变化，先核实，不要仅为绕过检查随意创建空目录或拷贝错误版本。**

同一 VM 的另一个安全 shell 使用相同 Token，按此顺序：

| 顺序 | API | 备注 |
|---|---|---|
| 1 | GET /v1/status | 仅确认 launcher 进程在运行 |
| 2 | GET /v1/output | 检查 HeadlessMC 控制台已就绪 |
| 3 | POST /v1/prepare，JSON {} | 本会话设置正确 mcdir、选择 DouDou |
| 4 | GET /v1/output | GET /v1/account 要返回 verified:true（必须本会话新写入已选账号文件）；同时检查输出没有报错 |
| 5 | POST /v1/launch，JSON {} | 仅此一次，执行上面 5G 的游戏命令 |
| 6 | GET /v1/output | 核查版本 .88、用户 DouDou、主菜单加载 |
| 7 | POST /v1/connect，JSON {} | 主菜单后连接预配置的服务器 |
| 8 | POST /v1/chat，JSON {"text":"Hello from DouDou"} | 先验证英文；再额外验证中文 |
| 9 | POST /v1/move，JSON {"direction":"forward","duration_ms":200} | 安全地面上验证位移 |

本地准备与启动调用示例：

~~~bash
curl -sS -X POST -H "Authorization: Bearer $DOUDOU_BRIDGE_TOKEN" \
  -H "Content-Type: application/json" -d '{}' \
  http://127.0.0.1:8765/v1/prepare

curl -sS -H "Authorization: Bearer $DOUDOU_BRIDGE_TOKEN" \
  http://127.0.0.1:8765/v1/account

curl -sS -H "Authorization: Bearer $DOUDOU_BRIDGE_TOKEN" \
  http://127.0.0.1:8765/v1/output

# /v1/account verified:true，且无控制台错误后：
curl -sS -X POST -H "Authorization: Bearer $DOUDOU_BRIDGE_TOKEN" \
  -H "Content-Type: application/json" -d '{}' \
  http://127.0.0.1:8765/v1/launch
~~~

若 select DouDou 失败，**暂停**，不要 launch：Muse 的手动成功路径是同一个启动器会话用 account -p offline login、按提示选择/登录 DouDou；不建议程序在无法核对原 UUID 时自动创建新离线身份。

## 已实现的 API 与边界

- GET /health：本机存活；
- GET /v1/status：Java 存活与是否提交了 prepare/launch（并不保证游戏主菜单已就绪）；
- GET /v1/output：最近最多 40 行完整输出 + partial（JLine 尚未换行提示符），退出码 exit_code，融合 stdout/stderr；异常时查看私有轮转诊断文件，不能把未经脱敏的日志发到公开 Issue；
- GET /v1/account：只报告本会话所选账号是否已在 last/.accounts.json 中写入，**不暴露 UUID/Token**，缺乏证据则拒绝 launch；
- GET /v1/events?after=0&wait_ms=20000：本机长期监听客户端 latest.log 中指定服主的 [CHAT] 行，以 sequence/cursor 增量读取，可长轮询最多 20 秒，最多缓存 256 条，检测到丢失会标记 dropped:true；
- POST /v1/prepare：安全固定目录、现有 DouDou 账号选择；
- POST /v1/launch：固定的 NeoForge 26.2 + 5G 指令，一次进程生命周期只准一次；
- POST /v1/connect：只连接预先设置的服务器；
- POST /v1/chat：1–200 字符，禁止换行/控制字符；
- POST /v1/move：前后左右 W/S/A/D，单次 50–1000ms 自动释放。

服务只监听 127.0.0.1:8765；/v1/* 使用 Bearer Token。不支持任意 shell 命令执行接口。HTTP 202 accepted:true, verified:false 只表示命令写入了 HeadlessMC 控制台；不代表游戏执行成功。

## 尚未完成

- 中文聊天是否真正无乱码；
- 本次加入当前会话账号状态文件验证，仍需 Muse 在真实 VM 确认文件路径和反馈生效；
- 现已实现日志级聊天事件入口，但尚未在 Muse VM 验证日志路径和端到端事件传输；坐标、背包数量、耐久、准星目标、实体/血量仍未结构化接入；
- 云 VM 重启后安全恢复、防并发重复启动、cron；
- NeoForge 实际加载各阶段的耗时分解。

参考：[Issue #2 真实两轮联调](https://github.com/Huhabubu/quant_test/issues/2)；[PR #3](https://github.com/Huhabubu/quant_test/pull/3)。


## 16:35 新回报：实时聊天与状态感知的正确边界

Muse 已验证：原有多级 worker 轮询会产生 1–2 分钟延迟；目前直接 tail -F latest.log 并唤醒主 agent，端到端约 **10–20 秒**，主要消耗在 Muse 本身。**本桥不承诺绕过 Muse 的唤醒/推理延迟。** 它提供不再依赖 shell 管道的本地增量事件缓冲。Muse 可以继续使用已经成功的 tail 机制，先测新 /v1/events 的延迟和完整性，再决定是否切换。

示例（在本地已注入同一 Token 的环境）：

~~~bash
curl -sS -G -H "Authorization: Bearer $DOUDOU_BRIDGE_TOKEN"   --data-urlencode "after=0" --data-urlencode "wait_ms=20000"   "http://127.0.0.1:8765/v1/events"
~~~

处理事件后持久记录响应里的 next_cursor，下一次用 after=<刚处理的序号>，不要重复回复相同的玩家消息；发现 dropped:true 要谨慎重同步，而不是猜测已处理哪句话。VM 重启后缓存与序号会从零开始，不可假定跨重启持续可用。

最新已验证的游戏内感知能力：Baritone 持续跟随、挖橡木；hmc-specifics gui 能列出背包格子物品**名称**，暂无法列出**数量**；工具损坏、准星目标不可直接读取。我们没有把这些信息伪装成已有的结构化 API。后续如需背包数量/耐久/准星目标，必须增加适配的 NeoForge 客户端状态通道。

### PTY 诊断与账号验证

- 终端未换行的 JLine 文本会在 /v1/output 的 partial 字段即时可见。JLine 的重复绘制提示可能仍含 ANSI 序列；
- Launcher 退出码在 /v1/output 的 exit_code 和 /v1/status 的 launcher_exit_code；标准错误与标准输出在 PTY 上合并；
- 当前进程输出会写入仅所有者可读的 ~/ .local/state/doudou-bridge/launcher.pty.log（实际路径为 ~/.local/state/doudou-bridge/launcher.pty.log），大于 2MB 后轮转为 launcher.pty.previous.log。日志可能包含玩家聊天，分享前脱敏；
- 启动时 --account-state-file 必须指向 HeadlessMC **实际使用**的 last/.accounts.json。上例 /root/... 是 Muse 曾报告的路径，不是硬编码事实；若运行用户或存储路径不同，应先由 Muse 核查。prepare 后 /v1/account 返回 verified:true 才允许 launch，避免仍用 Offline 账号。
