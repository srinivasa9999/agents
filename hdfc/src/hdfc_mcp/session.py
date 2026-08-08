"""Local persistence for the InvestRight access token.

The InvestRight login flow (token-id -> credentials -> 2FA -> authorise ->
access-token) requires a human to type an OTP, so it cannot be silently
re-run on every process start. We cache the resulting access token on disk
(user-only permissions) and reuse it until the API tells us it's expired.
"""
from __future__ import annotations

import json
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class StoredSession:
    access_token: str
    issued_at: float
    login_id: str | None = None


class SessionStore:
    def __init__(self, path: Path):
        self._path = path

    def load(self) -> StoredSession | None:
        try:
            raw = self._path.read_text()
        except FileNotFoundError:
            return None
        try:
            data = json.loads(raw)
            return StoredSession(
                access_token=data["access_token"],
                issued_at=data["issued_at"],
                login_id=data.get("login_id"),
            )
        except (KeyError, ValueError, json.JSONDecodeError):
            return None

    def save(self, access_token: str, login_id: str | None = None) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "access_token": access_token,
            "issued_at": time.time(),
            "login_id": login_id,
        }
        tmp_path = self._path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload))
        os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
        tmp_path.replace(self._path)

    def clear(self) -> None:
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
