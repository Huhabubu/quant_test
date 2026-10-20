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

1. **启动到了错误的 Minecraft 目录**：此前只配置了 --cwd，HeadlessMC 落到 /root/.minecraft，选了 .93，重新下载 481MB 且缺少 mod。现在强制 --mcdir 参数，在启动 Java 前验证现有模组实例，并向 launcher JVM 注入 hmc.mcdir 和 hmc.files.mc 两个系统属性。再通过同会话 /v1/prepare 显式发送 config set hmc.mcdir。
2. **账号变成 Offline**：保留上次账号记录并不代表当前 HeadlessMC 会话已选择 DouDou。/v1/prepare 发送 account -p offline select DouDou，使用**已有账号**，不重新生成 UUID。**如果该命令报错，不要继续 launch**。Muse 已验证 account -p offline login 的手动交互流程；若 select 未能恢复账号，请在 Issue 留下脱敏失败日志。
3. **模组缺失 / JourneyMap 崩溃**：预检检查现有 NeoForge 目录、足量 JAR、hmc-specifics、World Bosses、GeckoLib、fml.toml 的 earlyWindowControl=false，发现启用 JourneyMap 就拒绝启动。不删除或移动用户的文件。
4. **中文聊天发送乱码**：Java launcher 的输入、输出、文件编码增加 UTF-8 属性；PTY 的非 UTF-8 locale 自动修正，添加模拟中文往返测试。**不能宣称游戏内乱码已修复，必须由 Muse 和服主验证**。
5. **反复慢启动**：先排除错误目录、重复下载和崩溃造成的重复冷启动。原正常 NeoForge 启动仍约需数分钟，还未提供逐阶段 CPU/IO 耗时数据。

## 测试（不会启动真实 MC）

在 quant_test 仓库的 feature/doudou-minecraft-bridge 分支运行：

~~~bash
python3 -m unittest -v test_doudou_bridge.py test_stdio_bridge.py test_pty_bridge.py test_doudou_preflight.py
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
| 4 | GET /v1/output | **人工或 Muse 检查选账号是否出现报错** |
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
  http://127.0.0.1:8765/v1/output

# 确认本会话账号选择无错误后：
curl -sS -X POST -H "Authorization: Bearer $DOUDOU_BRIDGE_TOKEN" \
  -H "Content-Type: application/json" -d '{}' \
  http://127.0.0.1:8765/v1/launch
~~~

若 select DouDou 失败，**暂停**，不要 launch：Muse 的手动成功路径是同一个启动器会话用 account -p offline login、按提示选择/登录 DouDou；不建议程序在无法核对原 UUID 时自动创建新离线身份。

## 已实现的 API 与边界

- GET /health：本机存活；
- GET /v1/status：Java 存活与是否提交了 prepare/launch（并不保证游戏主菜单已就绪）；
- GET /v1/output：最近最多 40 行控制台输出，需要鉴权，上传公开 Issue 前要脱敏；
- POST /v1/prepare：安全固定目录、现有 DouDou 账号选择；
- POST /v1/launch：固定的 NeoForge 26.2 + 5G 指令，一次进程生命周期只准一次；
- POST /v1/connect：只连接预先设置的服务器；
- POST /v1/chat：1–200 字符，禁止换行/控制字符；
- POST /v1/move：前后左右 W/S/A/D，单次 50–1000ms 自动释放。

服务只监听 127.0.0.1:8765；/v1/* 使用 Bearer Token。不支持任意 shell 命令执行接口。HTTP 202 accepted:true, verified:false 只表示命令写入了 HeadlessMC 控制台；不代表游戏执行成功。

## 尚未完成

- 中文聊天是否真正无乱码；
- select 已保存账号是否能在未来新的 Muse 会话正确生效（如不能，需要补更稳的安全登录方案）；
- 实时结构化读取聊天、坐标、实体、血量；
- 云 VM 重启后安全恢复、防并发重复启动、cron；
- NeoForge 实际加载各阶段的耗时分解。

参考：[Issue #2 真实两轮联调](https://github.com/Huhabubu/quant_test/issues/2)；[PR #3](https://github.com/Huhabubu/quant_test/pull/3)。
