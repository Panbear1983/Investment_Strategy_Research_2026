#!/usr/bin/env python3
"""Roster of Telegram users allowed to chat with the investment-research gateway.

This is the data model behind the roster dashboard (telegram_roster_tui.py): an
ordered list of Telegram users — Dad is #1 — each routed to a Hermes profile that
carries the Codex OAuth. Stores NO credentials; the bot token and Codex login stay
in Hermes' credential store. Applying the roster writes Hermes gateway
`profile_routes`; it never writes a bot token or an LLM key.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from typing import Any

REPO_DIR = Path(__file__).resolve().parents[1]
USERS_PATH = REPO_DIR / "scripts" / "telegram_users.json"

# New read-only users route to the approved researcher role. The legacy reader
# profile is not an implicit fallback, so it can never be repurposed into admin
# and accidentally receive a future Telegram user.
DEFAULT_PROFILE = "researcher"
EDITOR_PROFILE = "investment-research-editor"

# Access tier -> the Hermes profile (sandbox) that grants that permission. The tier
# is what you pick per user in the roster; the profile is where read-only vs
# read-write actually lives (the Docker mount). read_write points at a profile that
# mounts a COPY of the dataset :rw, so the live 100k DB the loop uses stays safe.
ACCESS_TIERS = {
    "read_only": DEFAULT_PROFILE,    # dataset :ro, no writable mounts, no network
    "read_write": EDITOR_PROFILE,    # dataset copy :rw (isolated from the live database)
}
DEFAULT_COMMANDS = ("new", "stop", "status")
LANGUAGES = ("english", "traditional_chinese", "bilingual")
_CHAT_ID_RE = re.compile(r"[1-9][0-9]{4,19}")
_PROFILE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{1,63}")


@dataclasses.dataclass
class TelegramUser:
    label: str
    chat_id: str
    access: str = "read_only"
    profile: str = ""          # blank = the access tier's default profile (see ACCESS_TIERS)
    language: str = "bilingual"
    enabled: bool = True
    allowed_commands: list[str] = dataclasses.field(default_factory=lambda: list(DEFAULT_COMMANDS))
    note: str = ""
    # Per-person daily video allowance for 爸菲特. None = the config's video_daily_cap_per_user.
    # Peter gave four of his five to Dad on 2026-09-03: Dad 9, Peter 1.
    video_cap: int | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TelegramUser":
        fields = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in fields})

    def resolved_profile(self) -> str:
        """The Hermes profile this user routes to: an explicit profile override if
        set, otherwise the default profile for their access tier."""
        return self.profile.strip() or ACCESS_TIERS.get(self.access, DEFAULT_PROFILE)

    def validate(self) -> None:
        if not self.label.strip():
            raise ValueError("Each Telegram user needs a label")
        if not _CHAT_ID_RE.fullmatch(self.chat_id):
            raise ValueError(f"{self.label}: chat_id must be a numeric Telegram ID")
        if self.access not in ACCESS_TIERS:
            raise ValueError(f"{self.label}: access must be one of {', '.join(ACCESS_TIERS)}")
        if self.profile and not _PROFILE_RE.fullmatch(self.profile):
            raise ValueError(f"{self.label}: profile override must be a lowercase slug (a-z, 0-9, _, -)")
        if self.language not in LANGUAGES:
            raise ValueError(f"{self.label}: language must be one of {', '.join(LANGUAGES)}")
        if self.video_cap is not None and (
                isinstance(self.video_cap, bool) or not isinstance(self.video_cap, int)
                or self.video_cap < 0):
            raise ValueError(f"{self.label}: video_cap must be a whole number of videos per day")

    def route(self) -> dict[str, str]:
        """Hermes gateway route mapping this user's chat_id to their resolved profile."""
        slug = re.sub(r"[^a-z0-9]+", "-", self.label.lower()).strip("-") or "user"
        return {
            "name": f"tg-{slug}-{self.chat_id}",
            "platform": "telegram",
            "chat_id": self.chat_id,
            "profile": self.resolved_profile(),
        }


@dataclasses.dataclass
class Roster:
    users: list[TelegramUser]

    def validate(self) -> None:
        seen: set[str] = set()
        for user in self.users:
            user.validate()
            if user.chat_id in seen:
                raise ValueError(f"Duplicate chat_id {user.chat_id} in roster")
            seen.add(user.chat_id)

    def routes(self) -> list[dict[str, str]]:
        """Routes for enabled users only — disabled users are configured but not served."""
        return [u.route() for u in self.users if u.enabled]

    # --- ordered CRUD (order matters: index 0 is "first on the list") ---
    def add(self, user: TelegramUser) -> None:
        if any(u.chat_id == user.chat_id for u in self.users):
            raise ValueError(f"A user with chat_id {user.chat_id} already exists")
        user.validate()
        self.users.append(user)

    def remove(self, index: int) -> TelegramUser:
        return self.users.pop(index)

    def move(self, index: int, delta: int) -> int:
        new = max(0, min(len(self.users) - 1, index + delta))
        self.users.insert(new, self.users.pop(index))
        return new


def default_roster() -> Roster:
    """Seed roster: Dad is the first user, on the approved researcher profile."""
    return Roster([
        TelegramUser(
            label="Dad",
            chat_id="7108285456",
            profile="researcher",
            language="bilingual",
            note="First user",
        )
    ])


def load_users(path: Path = USERS_PATH) -> Roster:
    path = Path(path)  # callers may pass a str (e.g. dashboard's TG_USERS_PATH)
    if not path.exists():
        return default_roster()
    raw = json.loads(path.read_text(encoding="utf-8"))
    rows = raw.get("users", raw) if isinstance(raw, dict) else raw
    roster = Roster([TelegramUser.from_dict(r) for r in rows])
    roster.validate()
    return roster


def save_users(roster: Roster, path: Path = USERS_PATH) -> None:
    roster.validate()
    path = Path(path)  # callers may pass a str
    payload = {"users": [dataclasses.asdict(u) for u in roster.users]}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def build_profile_routes(roster: Roster, extra: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Full gateway.profile_routes list: this roster's routes plus any non-telegram
    routes to preserve. Telegram routes not owned by the roster are dropped."""
    owned = roster.routes()
    owned_ids = {r["chat_id"] for r in owned}
    kept = [
        r for r in (extra or [])
        if r.get("platform") != "telegram" or str(r.get("chat_id", "")) not in owned_ids
    ]
    return owned + kept
