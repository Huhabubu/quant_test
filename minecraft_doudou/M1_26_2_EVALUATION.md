# M1 26.2 NeoForge isolated compatibility experiment

Status: ONLY a safe local TCP probe and instructions have been written. No M1 jar has been installed or run on Muse VM and no in-game result is claimed. Keep proven DouDou v0.2.2 (or sleep-diagnostic v0.2.4) installed until the next authorized cold start.

## Official upstream sources

- M1 releases: https://modrinth.com/mod/m1-machine-one-ai-interface
- Correct target: 0.17.5+26.2-neoforge, NOT 0.17.5+26.2 (Fabric).
- Browse the correct release: https://modpacks.ch/mods/minecraft/7xdOhfw6/m1-machine-one-ai-interface?loader=neoforge&version=26.2
- Source and wire protocol: https://github.com/Kishku7/m1/tree/minecraft-1.20-26.3
- Command grammar / safety: https://github.com/Kishku7/m1/blob/main/README.md
- M1 license ARR, not an open license to copy implementation code.

M1 listens on 127.0.0.1:26000, TCP newline text, and replies end at a line exactly equal to <<END. Clients must consume the greeting before sending commands. RAW ON is the optional protocol initialization. M1's MCP bridge has no authentication and its HTTP BIND_HOST defaults to 0.0.0.0: DO NOT start that bridge for this experiment; use the localhost TCP probe only.

M1 documents face, look, use/place, attack/defend, navigation and sleep. None of those actions are yet verified on the user's HeadlessMC 26.2 LWJGL instance. IMPORTANT: automatic armor upgrades are ON by default and can modify an inventory without explicit action commands.

## Safety gates (Muse must apply)

1. Never hot-swap the currently running Minecraft mods or kill an unknown orphan Java process. Wait for a normal offline window.
2. Prefer an isolated disposable world/account/instance; do NOT run a second Java client concurrently on memory-limited VM. In multiplayer, M1 defaults and action side effects are too risky for initial actuation tests.
3. Download the published 0.17.5+26.2-neoforge JAR from the correct Modrinth page, record its actual SHA256, verify inside JAR the NeoForge mod id m1 and MC 26.2 dependency. Do NOT substitute a Fabric, Forge or 26.3 build.
4. Back up the existing mods list and the already verified DouDou client JAR. Do not remove that working jar; install exactly ONE M1 jar in the NEXT offline launch. Preserve game memory heap -Xmx3G.
5. Before probing, confirm ss -ltnp shows port 26000 bound by the intended Minecraft client and no bind collision; no external bind/forwarding. Do not start its unauthenticated MCP HTTP service.
6. No use, mining, chat command, pathfinding, item transfer or combat until read-only pass. Automatic armor upgrades can trigger without a command: test with disposable inventory first.

## Stage A: read-only handshake and status

After NeoForge logs confirm both DouDou and M1 loaded, execute from quant_test repo:

    python3 -m minecraft_doudou.m1_probe

The Python probe connects ONLY to 127.0.0.1:26000 and sends exactly RAW ON, help, where, look and inv. It consumes the greeting, buffers TCP beyond complete sentinel replies, has a 15-second timeout, and cannot send any action verb.

Pass conditions: no new crash for at least 2 minutes, listener present, genuine greeting, valid help/where/look/inv replies (where may be unavailable before joining a world), current DouDou state.json still updates, no inadvertent inventory/armor changes or movement. A TCP connection ALONE is not proof of game actuation.

Redact player names, coordinates, item lists, server details and any private content before copying probe JSON to public GitHub Issue. M1 output has no bearer authentication.

Failure categories: no_listener, bind_conflict, wrong_service_banner, reply_timeout, read_only_command_error, game_crash, side_effect, or socket_ok_but_not_in_world.

## Stage B: actuation ONLY after explicit approval and Stage A pass

In a disposable single-player world with no valuable items:
- Operator manually sends autoupgrade off, defend off; inspect command confirmations. Remember M1 auto armor was on until this was applied.
- Query where yaw/pitch, then issue face 90 0 and query where + look again. Judge actual in-game yaw delta, not merely an OK message.
- On sacrificial block with clear line-of-sight, inspect look, then manually test agent use / place; verify resulting world/screen state.
- Only after another explicit approval consider attack against one NON-PLAYER in disposable world. Never target real players.
- Optional: one test of M1 sleep against a fresh disposable bed and compare with DouDou 0.2.4 diagnostic output. Do not assume M1's advertised sleep works.
- Keep Baritone at #stop. Never run two navigation/controllers simultaneously.

## Decision

A fails: next safe offline window remove ONLY M1 and return to proven DouDou.
A passes, B fails: M1 only useful as a read-only/reference interface.
A+B pass: design a strict allowlisted adapter; retain DouDou Corpse ownership, landmark scopes, bounded action receipts, and verified server outcomes.
M1 TCP does not automatically wake Muse main agent; no speculative webhook integration.

## Muse response template

- M1 JAR version, real SHA256, Minecraft/NeoForge versions:
- isolated test vs actual server, stable running for 2 minutes:
- loading log, bind address and M1 probe status:
- redacted where/look/help/inv output:
- presence/absence of armor change without action:
- DouDou existing state compatibility:
- yaw before/after (Stage B only):
- use/attack real game effect (Stage B only):
- crashes/conflicts, recommended next step:

IMPORTANT: this experiment cannot directly attach to an already-running HeadlessMC client that did not load M1 at startup. No game/client was modified during preparation.
