"""Safe Python-side mailbox for the optional DouDou NeoForge client mod.

Local, owner-only JSON files; never writes memory or arbitrary shell commands.
The Java client handles requests on the Minecraft game thread. A dispatch is
*not* proof the server accepted item use, opened a screen or permitted sleep.
"""
import json
import os
from pathlib import Path
import time
from uuid import uuid4


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
        if not isinstance(state.get("in_world"), bool):
            raise ModUnavailable("游戏状态缺少 in_world")
        return state

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
        elif action in ("open_container", "sleep"):
            if set(request) != {"action", "x", "y", "z"}:
                raise InvalidModAction("方块交互需要 action/x/y/z")
            for key, bound in (("x", 30_000_000), ("y", 2048), ("z", 30_000_000)):
                value = request[key]
                if type(value) is not int or not -bound <= value <= bound:
                    raise InvalidModAction(f"{key} 必须在安全整数范围内")
        else:
            raise InvalidModAction("只允许 eat/open_container/sleep")
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
        payload = json.dumps(checked, ensure_ascii=False, separators=(",", ":")).encode()
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
            return {"status": "unknown_after_claim", "id": id_,
                    "note": "客户端已领取请求但无结果；不要自动重试使用动作"}
        return {"status": "not_found", "id": id_}
