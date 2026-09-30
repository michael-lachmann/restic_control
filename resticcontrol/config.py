"""Persistent app settings. Passwords go to the macOS Keychain (via `keyring`)."""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from typing import Optional

from .backend import RepoSpec

KEYCHAIN_SERVICE = "ResticControl"
_PASSWORDS: dict = {}                    # repo id -> password, cached for this process

DEFAULT_SFTP_ARGS = "-o sftp.args='-oBatchMode=yes -oServerAliveInterval=30'"


def config_path() -> str:
    base = os.path.expanduser("~/Library/Application Support")
    if not os.path.isdir(base):
        base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return os.path.join(base, "ResticControl", "config.json")


@dataclass
class RepoConfig:
    name: str = "New repository"
    repository: str = "sftp:user@host:/srv/restic-repo"
    password_command: str = ""          # alternative to storing the password in Keychain
    restic_path: str = ""               # empty = auto-detect
    extra_args: str = DEFAULT_SFTP_ARGS
    env: dict = field(default_factory=dict)   # e.g. {"RESTIC_CACHE_DIR": "..."}
    host_filter: str = ""               # remember last host selection
    index_all: bool = True              # index every snapshot in the background, not just the newest
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    # -- keychain ---------------------------------------------------------
    def get_password(self) -> Optional[str]:
        """Read from the Keychain at most once per launch (each read may prompt)."""
        if self.id in _PASSWORDS:
            return _PASSWORDS[self.id]
        try:
            import keyring
            pw = keyring.get_password(KEYCHAIN_SERVICE, self.id)
        except Exception:
            return None
        _PASSWORDS[self.id] = pw
        return pw

    def set_password(self, pw: Optional[str]) -> None:
        import keyring
        _PASSWORDS[self.id] = pw or None
        if pw:
            keyring.set_password(KEYCHAIN_SERVICE, self.id, pw)
        else:
            try:
                keyring.delete_password(KEYCHAIN_SERVICE, self.id)
            except Exception:
                pass

    def spec(self) -> RepoSpec:
        return RepoSpec(repository=self.repository,
                        password=None if self.password_command else self.get_password(),
                        password_command=self.password_command,
                        restic_path=self.restic_path,
                        extra_args=self.extra_args,
                        env=dict(self.env))


@dataclass
class AppConfig:
    repos: list = field(default_factory=list)
    selected_id: str = ""
    backrest_url: str = "http://127.0.0.1:9898"

    @property
    def selected(self) -> Optional[RepoConfig]:
        for r in self.repos:
            if r.id == self.selected_id:
                return r
        return self.repos[0] if self.repos else None

    @classmethod
    def load(cls) -> "AppConfig":
        try:
            with open(config_path()) as f:
                d = json.load(f)
        except (OSError, ValueError):
            return cls()
        known = set(RepoConfig.__dataclass_fields__)
        repos = [RepoConfig(**{k: v for k, v in r.items() if k in known}) for r in d.get("repos", [])]
        return cls(repos=repos, selected_id=d.get("selected_id", ""),
                   backrest_url=d.get("backrest_url", cls.backrest_url))

    def save(self) -> None:
        fn = config_path()
        os.makedirs(os.path.dirname(fn), exist_ok=True)
        tmp = fn + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"repos": [asdict(r) for r in self.repos],
                       "selected_id": self.selected_id,
                       "backrest_url": self.backrest_url}, f, indent=2)
        os.replace(tmp, fn)
