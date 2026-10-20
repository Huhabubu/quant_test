# DouDou 0.2.3 — Persistent HeadlessMC Console (Muse VM acceptance)

Status: Python-only candidate. Java Minecraft client Mod remains the
real-server verified doudou_client-0.2.2.jar. NO new JAR is needed.

## Why this change

Muse reported that its temporary controller terminal handle disappeared
while Minecraft was still running. A Python PTY wrapper cannot later
reconstruct ownership of a lost PTY master descriptor. Killing the game
was previously the only recovery method. Under 0.2.3:

- The HeadlessMC Java launcher lives in an independently persistent tmux
  session named doudou-managed.
- The localhost HTTP bridge only CONNECTS to that tmux session. If the bridge
  is lost or restarted, the launcher and Minecraft are not stopped.
- launch may be sent just once per tmux session. A tmux user option persists
  this across HTTP bridge restarts, with an OS file lock to serialize updates.
- The manager's ensure checks the tmux session, Mod heartbeat, and read-only
  candidate Java processes before creating a session. It never kills a process.
- Game JVM heap is fixed to the last successfully tested -Xmx3G, NOT 5G.
- Stale heartbeat does NOT cause automatic Java process termination or restart.
- Does not set up a system boot service; a genuinely rebooted VM must invoke
  ensure again, or later have a separately approved host-level boot unit.

## Preconditions

Inspect the live VM first. DO NOT run ensure if another manual HMC/PTY session
is already controlling a real Minecraft process. Existing processes may evade
/proc identification. A report of no_known_live_instance is not proof of no
game process.

The paths here are examples from older Muse reports, and may have changed.
Verify exact headlessmc.jar, game directory, Java 25, and account state paths.
The existing client must have stopped naturally before migrating control.

    cd /home/hatch/workspace/quant_test
    HMC_HOME=/home/hatch/workspace/headlessmc
    MC_DIR="$HMC_HOME/gamedir"

    python3 -m minecraft_doudou.session_manager status --mcdir "$MC_DIR"

Only on a confirmed safe, client-offline cold start:

    python3 -m minecraft_doudou.session_manager ensure \
      --mcdir "$MC_DIR" --cwd "$HMC_HOME" -- \
      "$HMC_HOME/jdk/jdk-25.0.4.1+1/bin/java" \
      -jar "$HMC_HOME/headlessmc.jar"

This creates ONE HeadlessMC launcher, NOT a logged-in Minecraft client.
Run the same command again: it must return already_present without
spawning a duplicate Java process.

Read or interact with the same persistent terminal:

    tmux capture-pane -p -t '=doudou-managed' -S -40
    tmux attach-session -t '=doudou-managed'

To detach without killing Java: Ctrl-b then d. Do not type exit/quit if you
intend to preserve the launcher. Console output may contain private player
chats: redact before sharing logs.

## Reconnectable authenticated HTTP bridge

In a SEPARATE terminal, running from the repository root:

    export DOUDOU_BRIDGE_TOKEN='<existing_secret_at_least_32_chars>'
    python3 -m minecraft_doudou.bridge \
      --tmux-session doudou-managed \
      --mcdir "$MC_DIR" \
      --account-state-file '<actual_LastUsedAccountService_.accounts.json_path>' \
      --owner-name Huhabubu \
      --server-host '<private_server_host>' --server-port '<port>'

The HTTP bridge binds ONLY to 127.0.0.1:8765 and requires a bearer token
for all /v1/ operations. Never place the token in the public GitHub Issue.
If this Python bridge process stops, simply rerun the SAME bridge command
using the same tmux session; the Minecraft client must not be restarted.

On the first new launcher run, inspect GET /v1/status and /v1/output, then
POST /v1/prepare with {}, check GET /v1/account returns verified:true
and the console contains no selection errors, then POST /v1/launch {} ONCE.
That action issues the known NeoForge 26.2, -lwjgl, -eula, -Xmx3G command.
Once the main menu actually loads, POST /v1/connect {} to the configured server.
Both launch and connect merely dispatch commands: confirm actual game state.

On reattaching to an already playing client, GET /v1/status should show
console_available=true and game_state_fresh=true, server_connected=true
when the Mod is in world. DO NOT call prepare, launch or connect again.

If the account cannot be verified, STOP. Do not create a new offline account
or let the wrong UUID join the server. The AccountSelectionProbe checks the
active session's current selection record, not simply an old saved account.

## Acceptance tests on Muse VM

1. While DouDou is online through the older setup, run status ONLY. There
   must be no new tmux/Java process. Wait for normal offline/cold start.
2. First ensure starts exactly one new tmux launcher; second ensure does
   not spawn a second launcher. Check correct game directory and UTF-8.
3. After verified account selection, launch with -Xmx3G and enter the world.
4. Kill ONLY the local HTTP bridge (not tmux or Minecraft), then restart
   it and verify the same tmux session/Java PID responds to /v1/output.
5. A second launch request after bridge restart MUST reject. Chat/movement
   and Mod state continue to work in the existing client.
6. During any stale state.json event, health must say unknown/stale, NOT
   assume the game is dead or restart/kill Java.
7. In a real VM reboot both processes may disappear; verify that a
   subsequent manually invoked ensure can create one new console. True
   unattended system-boot restart is a separate feature, NOT proven here.

Known limitation: this protects the console ONLY when launched under the
managed tmux terminal from the outset. It cannot magically reattach the
stdin of an already-orphaned process launched with a transient PTY. Such
a process must be handled manually, and it must never be killed automatically.

Not in scope: Baritone digging holes, automated combat, GUI mouse emulation,
event-to-Muse autonomous wake-ups, persistent cloud VM hosting.
