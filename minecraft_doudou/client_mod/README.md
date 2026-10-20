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
- The installed 0.1.0 JAR does not update by changing the Python files. The expanded-perception build is version 0.1.1; remove the old 0.1.0 JAR before installing 0.1.1 to avoid a duplicate mod ID. Obtain a new build candidate and install during the NEXT safe cold boot; do not hot-swap a live mod.

## v0.2.0 — Durable landmarks and moving entity perception

**Important:** the installed v0.1.0/v0.1.1 clients do NOT provide these capabilities. Compile or download the new v0.2.0 candidate and install it only after the live client has naturally stopped. Remove/archive every older doudou_client-*.jar before adding the new jar; never run multiple copies of the same mod ID.

### Remembered static landmarks (even when DouDou walks away)

- Every 40 client ticks (~2 s at 20 TPS), the existing 12-block loaded-chunk scan detects supported **vanilla beds, chests, barrels, shulker boxes**, and workstation blocks including **crafting table, furnace, blast furnace, smoker, smithing table, anvil, stonecutter, grindstone, brewing stand, enchanting table, loom, cartography table and fletching table**.
- These observations are held in memory and atomically persisted at `gamedir/neoforge-26.2/config/doudou-io/landmarks.json`. Disk writes occur for added/removed landmarks, or at most once every ~30 s for timestamp-only refreshes. Maximum 1,024 locations across all recognized worlds.
- Memory is scoped by a SHA-256 hash of the **connected server endpoint and dimension**, preventing overworld/Nether or distinct known servers from mixing. If the client cannot determine a remote server endpoint, `landmark_memory_available=false` and it does not claim durable cross-world memory.
- A new snapshot field `remembered_landmarks` reports up to 256 recently observed places, including `x/y/z`, block type, kind, dimension, and `last_seen_at_ms`. `seen_in_latest_scan=true` means the block was present during the most recent scan; **neither this nor an old memory entry guarantees the block is present now**.
- On revisiting a loaded, scanned area, if a recorded block has been broken/replaced, the record is removed. Locations in unloaded or distant chunks remain as **unverified historical observations** until revisited.
- A separate optional lookup API `GET /v1/landmarks?kind=bed&limit=100` returns historical bed/container/workstation coordinates without needing them to be in the current 12-block scan, scoped to the present world. The independent CLI is `python3 -m minecraft_doudou.mod_cli --root "$ROOT" landmarks --kind bed`.
- **Navigation safety:** Do not send a bed/chest/workstation's exact block coordinates as a Baritone `#goto` destination. Muse previously broke a bed by doing this. Select a proven safe adjacent standing position, and use a no-block-break pathfinding policy before approaching valuable blocks. This mod does NOT yet produce or guarantee a safe stand point.
- Persistence lasts only as long as the underlying **gamedir** survives. If Muse resets the entire VM including the Minecraft files, historical landmarks will be lost unless the game directory is backed up.

### Live moving entities — NOT permanent landmarks

- Within **24 blocks** of DouDou, the client reads its **currently loaded entities** once every 20 client ticks (~1 s): other players (`player`), monster-category mobs (`hostile`), animals (`animal`), other loaded mobs such as villagers (`other_mob`), and ground item entities (`dropped_item`).
- `nearby_entities[]`: `category`, entity `type`, name, current `x/y/z`, and `distance`; for dropped item entities includes **item_id and count**.
- Also reports `nearby_entity_radius=24` and `nearby_entities_updated_at_ms`. Results are bounded to the **64 nearest** entities per scan. These are client-known positions, **not necessarily in line of sight**; no entities are pulled from unloaded chunks.
- Moving entity positions are deliberately **not saved in landmarks.json**. Presence/position can change between snapshots; Muse should not confidently say another player is still there based only on an old reading.

### Verification after installing v0.2.0

1. Check latest state snapshot: `landmark_memory_available`, `dimension`, `nearby_entity_radius=24`, `nearby_entities`, and `remembered_landmarks`.
2. Walk past a chest, a bed and crafting table; verify each is remembered with coordinates and block type. Walk more than 12 blocks away and query `/v1/landmarks?kind=bed` or `mod_cli landmarks --kind bed`: the old bed should remain listed with a past `last_seen_at_ms`.
3. After a natural client restart, query the same world again; the remembered landmark should return. If not, verify the game directory and scope identity were stable and landmarks.json was preserved.
4. Safely break and replace a **test-only** workstation. After a subsequent scan, confirm the old record is invalidated/updated. Do not destroy anyone else's structure for testing.
5. Near another consenting player, animal, hostile mob or tossed low-value item, confirm correct category, distance and item count. Confirm old entities vanish from the live list when they move away. Do not claim visibility through walls.
6. Measure tick-time overhead before enabling periodic autonomous reasoning. These snapshots **do not themselves wake Muse**; the event-to-Agent wake path remains a distinct task.

**Known separate bug, based on Muse's latest Issue:** `eat` currently selects the food slot but does not hold use long enough for consumption. Container opening and nighttime sleeping have been verified in v0.1.0; do not mark eating as fixed by this version.


## v0.2.1: Eating test candidate

The previously deployed 0.2.0 cannot finish eating. This version holds the game use-item control across several ticks instead of sending only one action. It releases the control on success, interruption or timeout. It checks the observed hunger level and food stack count before claiming success.

The game command result is deferred until the attempt finishes. Query the result ID as usual. During the attempt it can return `in_progress`; on completion it reports before/after food counts, hunger levels and ticks elapsed. A build passing CI is NOT proof that a real server accepted the eating action.

Muse next test: when the existing client has naturally stopped, install only the new JAR after archiving older versions. In a safe area, stop Baritone, close GUI windows, place golden carrots in the hotbar, ensure hunger is below 20, and submit one `eat` action. Check that the item stack decreases and hunger rises within several seconds. Do not automatically retry an unconfirmed action.
