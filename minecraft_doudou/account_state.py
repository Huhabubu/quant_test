"""Read-only proof that HeadlessMC selected the existing offline account.

HeadlessMC's LastUsedAccountService writes accounts.latest to
<authdir>/last/.accounts.json. We intentionally never read credential stores
or expose UUID/token. A stale correct file is not proof of THIS session.
"""

import json
from pathlib import Path


class AccountSelectionProbe:
    def __init__(self, path, name="DouDou"):
        self.path = Path(path)
        self.name = name
        self.baseline = None
        self.armed = False

    def arm(self):
        self.baseline = self._stamp()
        self.armed = True

    def _stamp(self):
        try:
            stat = self.path.stat()
            return (stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns)
        except OSError:
            return None

    def verify(self):
        if not self.armed:
            return {"verified": False, "reason": "not_prepared"}
        new_stamp = self._stamp()
        if new_stamp is None:
            return {"verified": False, "reason": "last_account_file_not_found"}
        if new_stamp == self.baseline:
            return {"verified": False, "reason": "no_selection_write_since_prepare"}
        try:
            with self.path.open("r", encoding="utf-8") as file:
                data = json.load(file)
            latest = data.get("accounts", {}).get("latest")
            if not isinstance(latest, list) or not latest or not isinstance(latest[0], dict):
                return {"verified": False, "reason": "unexpected_account_file_format"}
            first = latest[0]
            ok = first.get("provider") == "offline" and first.get("name") == self.name
            return {
                "verified": ok,
                "selected_name": first.get("name") if isinstance(first.get("name"), str) else None,
                "reason": "selected_in_current_session" if ok else "wrong_selected_account",
            }
        except (OSError, ValueError, TypeError, AttributeError):
            return {"verified": False, "reason": "account_file_unreadable"}
