# DouDou Client Control — NeoForge 26.2

This is an **experimental client-only mod** for the existing real Minecraft client. Do not install it into the server. It is designed for NeoForge 26.2.0.88 and Java 25.

The mod avoids the broken mouse simulation on Muse's headless client. Every ten client ticks it writes a real-time state snapshot to the instance's config/doudou-io/state.json, including: health, hunger, position, sleeping/dead/using-item status, inventory item **counts**, damage, max damage and remaining durability. If a container menu is open, it also exports slot contents and quantities.

It handles only three bounded actions:
- eat: choose edible food already in hotbar (optionally a specified zero-based slot 0–8) and dispatch the real client game-mode use-item action.
- open_container: interact with a nearby, unobstructed vanilla chest, barrel or shulker box identified by x/y/z.
- sleep: interact with a nearby, unobstructed vanilla bed identified by x/y/z. Server rules still apply.

**Compilation is not evidence that any in-game action succeeded.** Every action receipt reports whether the game thread dispatched it, not whether the server permitted it. Confirm item count/hunger changes, container-open state or sleeping state separately.

## Build

Use Gradle 9.2.1 and Java 25, from minecraft_doudou/client_mod:

~~~bash
gradle --no-daemon build
~~~

The branch has a separate GitHub Actions Java 25 compilation workflow. If compilation succeeds, the workflow produces the build/libs/doudou_client-0.1.0.jar file. Copy that file to the EXISTING gamedir/neoforge-26.2/mods folder only on a later controlled cold boot, after first checking no existing client is running. Keep hmc-specifics, Baritone, World Bosses and GeckoLib installed; do NOT re-enable JourneyMap.

## Python bridge endpoints

All requests use the existing Bearer token over 127.0.0.1:8765.

| Method and route | Body / purpose |
| --- | --- |
| GET /v1/game-state | Get a non-stale (<3 seconds) snapshot |
| POST /v1/game-action | JSON {"action":"eat","slot":4} (slot 4 is fifth hotbar cell) |
| POST /v1/game-action | JSON {"action":"eat"} (first hotbar food) |
| POST /v1/game-action | JSON {"action":"open_container","x":1,"y":64,"z":2} |
| POST /v1/game-action | JSON {"action":"sleep","x":1,"y":64,"z":2} |
| GET /v1/game-action?id=32hex | Poll queued/completed/unknown_after_claim/not_found |

Each request is bound to the current client session_id and expires after 10 seconds; old commands from a previous game process cannot execute on a restart. The client polls command files under config/doudou-io/commands. It **claims a request before execution** to prevent automatic replay after crashes. Unknown-after-claim is not safe to retry blindly. The client writes results atomically under config/doudou-io/results. The in-game client is the only executor; Python cannot issue arbitrary game actions or shell commands.

The filesystem must remain private to the Muse VM's local game/bridge user; the localhost HTTP port must not be exposed publicly.

## Muse integration / acceptance tests

1. Compile successfully on Java 25; ensure the .88 client starts with the mod and joins the existing server without a server mod change.
2. Verify item stack counts, remaining axe durability, hunger and health against the game.
3. **Stop Baritone first** for a controlled action test, to prevent concurrent inputs.
4. To test eating, be hungry, put golden carrot in hotbar, capture game-state before, issue eat ONCE, poll the action, and verify hunger/item count after enough ticks. If no change, report actual game logs and do not claim eating works.
5. To test chest, stand within 4.5 blocks of a chest with clear sight, identify its block coordinates, issue open_container, and check container_open and screen_items. Do not assume success from a dispatched receipt.
6. To test sleeping, interact with a bed at known coordinates during a permitted in-game time. Verify sleeping or server refusal. Do not spoof game rules.
7. Confirm missing food, blocked sight, wrong target, unloaded chunk, dead state and out-of-range requests are rejected without interaction.

At present, the mod does **not** autonomously choose which chest/bed to visit, move items from non-hotbar inventory to a usable slot, control camera pitch/yaw, or stop/resume Baritone. These are distinct future capabilities.


## 旧桥尚未修好时如何独立验证 Mod

Muse 之前的 PTY 桥曾在 startup 退出；它不应该阻止 Mod 的功能实机验证。只在下一次自然冷启动安装 Mod 后，可继续按 Muse **已经验证可入服的手动 HeadlessMC 流程**启动客户端，然后在同一 VM 的另一 shell 中使用 Python 本地 CLI。操作系统用户必须有游戏目录读写权限。

~~~bash
# 以下 GAME 路径仅是 Muse 曾报告的实例路径，需先核实现场值
GAME=/home/hatch/workspace/headlessmc/gamedir/neoforge-26.2
ROOT="$GAME/config/doudou-io"
python3 -m minecraft_doudou.mod_cli --root "$ROOT" state

# 先停止 Baritone，确认快捷栏第 5 格确实为可吃的食物，再提交一次：
python3 -m minecraft_doudou.mod_cli --root "$ROOT" eat --slot 4

# 从上一命令返回的 id 查询是否被客户端执行：
python3 -m minecraft_doudou.mod_cli --root "$ROOT" result <32hex-id>

# 从 state 里的 nearby_interactables 获取 usable_now=true 的真实方块坐标：
python3 -m minecraft_doudou.mod_cli --root "$ROOT" open 1 64 2
python3 -m minecraft_doudou.mod_cli --root "$ROOT" sleep 1 64 2
~~~

最后两个坐标仅为示意，**必须使用游戏内读到的实际 x/y/z**。如果 Action 回报 accepted=true,verified=false，仍要等待服务器状态变化再判断成功。

这样可以分别验证“Mod 本身能否执行”与“HTTP 桥能否启动和控制”，避免两类故障同时干扰诊断。所有动作受 session_id 和 10 秒有效期保护，跨客户端重启的旧命令不会被执行。

## Nearby perception radius — expanded source (requires a newly built JAR)

- Supported targets: vanilla chest, barrel, shulker box, bed. **Not** all terrain blocks, nearby players, hostile mobs, dropped items or unopened chest inventory contents.
- Previously: eye distance at most **6 blocks**, grid ±5 horizontal / ±3 vertical, at most 24 results, scanned every 10 client ticks.
- New: at most **12 blocks** from the eye, scan ±12 horizontal and ±6 vertical, return the **48 closest** results sorted by distance.
- The wider scan runs every **40 client ticks (~2 s at 20 TPS)**; health, item counts and durability remain updated every 10 ticks (~0.5s).
- State JSON includes nearby_scan_radius, nearby_scan_vertical_range and nearby_scan_updated_at_ms. Cached usable_now can be 2 seconds old; game actions still check the **4.5 block interaction reach** and line-of-sight before executing.
- Only ALREADY LOADED chunks are inspected; this does not force distant chunk loads.
- The installed 0.1.0 JAR does not update by changing the Python files. Obtain a new build candidate and install during the NEXT safe cold boot; do not hot-swap a live mod.
