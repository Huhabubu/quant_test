"""Safe Python-side mailbox for the optional DouDou NeoForge client mod.

Local, owner-only JSON files; never writes memory or arbitrary shell commands.
The Java client handles requests on the Minecraft game thread. A dispatch is
*not* proof the server accepted item use, opened a screen or permitted sleep.
"""
import json
import os
from pathlib import Path
import time
from uuid import uuid4, UUID


class ModUnavailable(RuntimeError):
    pass


class InvalidModAction(ValueError):
    pass


class ModMailbox:
    def __init__(self, root, *, freshness_ms=3000):
        self.root = Path(root)
        self.freshness_ms = freshness_ms

    def _read_json(self, path, max_bytes=131072):
        try:
            if path.stat().st_size > max_bytes:
                raise ModUnavailable("状态文件超过大小上限")
            with path.open("rb") as fp:
                return json.loads(fp.read(max_bytes + 1).decode("utf-8"))
        except (OSError, ValueError, UnicodeError, TypeError) as exc:
            raise ModUnavailable("客户端 Mod 尚未提供可读取的状态") from exc

    def state(self):
        state = self._read_json(self.root / "state.json")
        if not isinstance(state, dict) or state.get("schema") != 1:
            raise ModUnavailable("游戏状态 schema 不匹配")
        timestamp = state.get("captured_at_ms")
        if type(timestamp) is not int or abs(time.time() * 1000 - timestamp) > self.freshness_ms:
            raise ModUnavailable("游戏状态过期（可能已退出/客户端暂停）")
        session = state.get("session_id")
        if not isinstance(session, str) or len(session) != 32 or any(c not in "0123456789abcdef" for c in session):
            raise ModUnavailable("游戏状态缺少有效的会话标识")
        if not isinstance(state.get("in_world"), bool):
            raise ModUnavailable("游戏状态缺少 in_world")
        return state

    def landmarks(self, kind=None, limit=100):
        """Recall old fixed-block locations; each result remains unverified."""
        if kind is not None and kind not in ("bed", "container", "workstation"):
            raise InvalidModAction("kind 只能是 bed/container/workstation")
        if type(limit) is not int or not 1 <= limit <= 256:
            raise InvalidModAction("limit 必须是 1..256")
        state = self.state()
        scope = state.get("landmark_memory_scope")
        if not isinstance(scope, str) or len(scope) != 64 or any(
                ch not in "0123456789abcdef" for ch in scope):
            raise ModUnavailable("当前服务器或维度身份无法确认")
        saved = self._read_json(self.root / "landmarks.json", max_bytes=1_000_000)
        if not isinstance(saved, dict) or saved.get("schema") != 1:
            raise ModUnavailable("地标记忆格式不兼容")
        entries = saved.get("entries")
        if not isinstance(entries, list) or len(entries) > 1024:
            raise ModUnavailable("地标记忆记录无效")
        found = []
        for value in entries:
            if not isinstance(value, dict) or value.get("scope") != scope:
                continue
            if kind is not None and value.get("kind") != kind:
                continue
            if not all(type(value.get(c)) is int for c in ("x", "y", "z", "last_seen_at_ms")):
                continue
            found.append(value)
        found.sort(key=lambda v: v["last_seen_at_ms"], reverse=True)
        return {
            "entries": found[:limit], "count_matching": len(found),
            "truncated": len(found) > limit,
            "verification": "historical_last_seen_only_revalidate_before_use",
            "note": "地标坐标是方块本体，不可直接作为 Baritone #goto 站位",
        }

    @staticmethod
    def validate(request):
        if not isinstance(request, dict):
            raise InvalidModAction("需要 JSON 对象")
        action = request.get("action")
        if action == "eat":
            if not set(request).issubset({"action", "slot"}):
                raise InvalidModAction("eat 只接受可选 slot")
            if "slot" in request and (type(request["slot"]) is not int
                                      or not 0 <= request["slot"] <= 8):
                raise InvalidModAction("slot 必须是 0..8 的快捷栏索引")
        elif action in ("open_corpse", "take_corpse_items"):
            if set(request) != {"action", "entity_uuid"}:
                raise InvalidModAction("尸体操作仅接受 action/entity_uuid")
            identity = request["entity_uuid"]
            if not isinstance(identity, str) or len(identity) != 36:
                raise InvalidModAction("entity_uuid 必须是规范 UUID 字符串")
            try:
                parsed = UUID(identity)
            except (ValueError, AttributeError) as exc:
                raise InvalidModAction("entity_uuid 无效") from exc
            if str(parsed) != identity:
                raise InvalidModAction("entity_uuid 必须为小写、带连字符的规范 UUID")
        elif action in ("open_container", "sleep"):
            if set(request) != {"action", "x", "y", "z"}:
                raise InvalidModAction("方块交互需要 action/x/y/z")
            for key, bound in (("x", 30_000_000), ("y", 2048), ("z", 30_000_000)):
                value = request[key]
                if type(value) is not int or not -bound <= value <= bound:
                    raise InvalidModAction(f"{key} 必须在安全整数范围内")
        else:
            raise InvalidModAction("只允许 eat/open_container/sleep/open_corpse/take_corpse_items")
        return dict(request)

    def submit(self, request):
        checked = self.validate(request)
        state = self.state()
        if not state["in_world"] or state.get("dead"):
            raise ModUnavailable("不在可操作的游戏世界（或已经死亡）")
        commands = self.root / "commands"
        if not commands.is_dir() or not (self.root / "results").is_dir():
            raise ModUnavailable("客户端 Mod 队列未准备好")
        id_ = uuid4().hex
        final = commands / (id_ + ".json")
        temp = commands / ("." + id_ + ".tmp")
        payload = json.dumps(dict(checked, session_id=state["session_id"],
                                  created_at_ms=int(time.time() * 1000)),
                             ensure_ascii=False, separators=(",", ":")).encode()
        try:
            with temp.open("xb") as fp:
                os.chmod(temp, 0o600)
                fp.write(payload)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(temp, final)
        except OSError as exc:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise ModUnavailable("无法提交游戏动作") from exc
        return {"accepted": True, "verified": False, "id": id_,
                "note": "仅证明请求已排队；通过 /v1/game-action?id=... 查询游戏端处理结果"}

    def result(self, id_):
        if not isinstance(id_, str) or len(id_) != 32 or any(c not in "0123456789abcdef" for c in id_):
            raise InvalidModAction("无效请求 ID")
        out = self.root / "results" / (id_ + ".json")
        if out.exists():
            value = self._read_json(out)
            if not isinstance(value, dict) or value.get("id") != id_:
                raise ModUnavailable("游戏结果记录不一致")
            return {"status": "completed", "result": value}
        pending = self.root / "commands" / (id_ + ".json")
        if pending.exists():
            return {"status": "queued", "id": id_}
        processing = self.root / "commands" / (id_ + ".processing")
        if processing.exists():
            try:
                current = self.state()
                if current.get("eating_active") is True and current.get("eating_id") == id_:
                    return {
                        "status": "in_progress", "id": id_,
                        "ticks_elapsed": current.get("eating_ticks_elapsed", 0),
                        "note": "客户端仍在持续使用食物，请等待最终确认，勿重复提交",
                    }
            except ModUnavailable:
                pass
            return {"status": "unknown_after_claim", "id": id_,
                    "note": "客户端已领取请求但无结果；不要自动重试使用动作"}
        return {"status": "not_found", "id": id_}
