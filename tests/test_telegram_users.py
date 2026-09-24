"""Tests for the Telegram user roster (multi-user gateway configuration)."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import telegram_users as tu


class RosterModelTests(unittest.TestCase):
    def test_dad_is_first(self):
        r = tu.default_roster()
        self.assertEqual(r.users[0].label, "Dad")
        self.assertEqual(r.users[0].chat_id, "7108285456")
        self.assertEqual(r.users[0].profile, "researcher")

    def test_add_and_ordering_keeps_dad_first(self):
        r = tu.default_roster()
        r.add(tu.TelegramUser(label="Alice", chat_id="123456789"))
        r.add(tu.TelegramUser(label="Bob", chat_id="223456789"))
        self.assertEqual([u.label for u in r.users], ["Dad", "Alice", "Bob"])
        # a new user can be promoted but Dad can be kept on top
        r.move(2, -1)  # Bob up one
        self.assertEqual([u.label for u in r.users], ["Dad", "Bob", "Alice"])

    def test_access_tier_resolves_to_profile(self):
        self.assertEqual(
            tu.TelegramUser(label="A", chat_id="111111111", access="read_only").resolved_profile(),
            tu.ACCESS_TIERS["read_only"])
        self.assertEqual(
            tu.TelegramUser(label="B", chat_id="222222222", access="read_write").resolved_profile(),
            tu.ACCESS_TIERS["read_write"])
        # explicit profile overrides the tier default (e.g. Dad on dad-investment)
        self.assertEqual(
            tu.TelegramUser(label="C", chat_id="333333333", access="read_only",
                            profile="researcher").resolved_profile(), "researcher")

    def test_read_write_routes_to_editor_profile(self):
        r = tu.Roster([tu.TelegramUser(label="Editor", chat_id="444444444", access="read_write")])
        self.assertEqual(r.routes()[0]["profile"], tu.EDITOR_PROFILE)

    def test_invalid_access_rejected(self):
        with self.assertRaises(ValueError):
            tu.TelegramUser(label="X", chat_id="123456789", access="superuser").validate()

    def test_missing_access_defaults_read_only(self):
        u = tu.TelegramUser.from_dict({"label": "Legacy", "chat_id": "555555555"})
        self.assertEqual(u.access, "read_only")
        self.assertEqual(u.resolved_profile(), tu.ACCESS_TIERS["read_only"])

    def test_duplicate_chat_id_rejected(self):
        r = tu.default_roster()
        with self.assertRaises(ValueError):
            r.add(tu.TelegramUser(label="Clone", chat_id="7108285456"))

    def test_validation_errors(self):
        with self.assertRaises(ValueError):
            tu.TelegramUser(label="", chat_id="123456789").validate()
        with self.assertRaises(ValueError):
            tu.TelegramUser(label="X", chat_id="not-numeric").validate()
        with self.assertRaises(ValueError):
            tu.TelegramUser(label="X", chat_id="123456789", profile="Bad Slug").validate()
        with self.assertRaises(ValueError):
            tu.TelegramUser(label="X", chat_id="123456789", language="klingon").validate()

    def test_routes_only_include_enabled_users(self):
        r = tu.default_roster()
        r.add(tu.TelegramUser(label="Alice", chat_id="123456789", enabled=False))
        routes = r.routes()
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]["chat_id"], "7108285456")
        self.assertEqual(routes[0]["platform"], "telegram")
        self.assertTrue(routes[0]["name"].startswith("tg-dad-"))

    def test_build_profile_routes_preserves_non_telegram_and_replaces_owned(self):
        r = tu.default_roster()
        extra = [
            {"name": "discord-x", "platform": "discord", "chat_id": "999", "profile": "p"},
            {"name": "stale-dad", "platform": "telegram", "chat_id": "7108285456", "profile": "old"},
        ]
        merged = tu.build_profile_routes(r, extra)
        tele = [x for x in merged if x["platform"] == "telegram"]
        disc = [x for x in merged if x["platform"] == "discord"]
        self.assertEqual(len(tele), 1)                 # stale telegram route replaced, not duplicated
        self.assertEqual(tele[0]["profile"], "researcher")
        self.assertEqual(len(disc), 1)                 # non-telegram route preserved

    def test_video_cap_is_optional_and_must_be_a_whole_number(self):
        tu.TelegramUser(label="A", chat_id="111111111").validate()             # None is fine
        tu.TelegramUser(label="A", chat_id="111111111", video_cap=9).validate()
        with self.assertRaises(ValueError):
            tu.TelegramUser(label="A", chat_id="111111111", video_cap=-1).validate()
        with self.assertRaises(ValueError):
            tu.TelegramUser(label="A", chat_id="111111111", video_cap="nine").validate()

    def test_video_cap_survives_a_save_and_load(self):
        r = tu.default_roster()
        r.users[0].video_cap = 9
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "telegram_users.json"
            tu.save_users(r, p)
            self.assertEqual(tu.load_users(p).users[0].video_cap, 9)

    def test_from_dict_ignores_unknown_keys(self):
        u = tu.TelegramUser.from_dict(
            {"label": "Y", "chat_id": "123456789", "bogus": "ignored", "profile": "reader"}
        )
        self.assertEqual(u.label, "Y")
        self.assertEqual(u.profile, "reader")

    def test_save_load_roundtrip(self):
        r = tu.default_roster()
        r.add(tu.TelegramUser(label="Alice 阿姨", chat_id="123456789", language="traditional_chinese"))
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "telegram_users.json"
            tu.save_users(r, p)
            back = tu.load_users(p)
        self.assertEqual([u.label for u in back.users], ["Dad", "Alice 阿姨"])
        self.assertEqual(back.users[1].language, "traditional_chinese")

    def test_load_missing_file_returns_default(self):
        with tempfile.TemporaryDirectory() as d:
            r = tu.load_users(Path(d) / "nope.json")
        self.assertEqual(r.users[0].label, "Dad")


class RosterModalTests(unittest.TestCase):
    """The roster editor now lives inside dashboard.py as an in-app modal (one TUI)."""

    def test_modal_boots_and_lists_dad(self):
        import asyncio
        import dashboard
        from textual.app import App
        from textual.widgets import DataTable

        _, RosterScreen = dashboard.run_tui(return_app=True)

        async def scenario(users_path):
            class Host(App):
                def on_mount(self):
                    self.push_screen(RosterScreen(users_path))

            app = Host()
            async with app.run_test() as pilot:
                await pilot.pause()
                table = app.screen.query_one("#rtable", DataTable)
                return table.row_count, table.get_row_at(0)

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "telegram_users.json"
            tu.save_users(tu.default_roster(), p)
            rows, row0 = asyncio.run(scenario(str(p)))  # str path, as dashboard passes it
        self.assertEqual(rows, 1)
        self.assertIn("Dad", row0)

    def test_add_and_remove_via_modal_handlers(self):
        """Drive the real button handlers (add/remove) end-to-end on a temp file."""
        import asyncio
        import dashboard
        from textual.app import App
        from textual.widgets import Button, DataTable, Input

        _, RosterScreen = dashboard.run_tui(return_app=True)

        def press(screen, bid):
            btn = screen.query_one(f"#{bid}", Button)
            screen.on_button_pressed(Button.Pressed(btn))

        async def scenario(users_path):
            out = {}

            class Host(App):
                def on_mount(self):
                    self.push_screen(RosterScreen(users_path))

            app = Host()
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = app.screen
                table = screen.query_one("#rtable", DataTable)
                out["start"] = table.row_count
                screen.query_one("#r-label", Input).value = "Alice"
                screen.query_one("#r-chat", Input).value = "123123123"
                press(screen, "r-upsert")
                await pilot.pause()
                out["after_add"] = table.row_count
                screen.selected = 1                       # select the new row
                press(screen, "r-remove")
                await pilot.pause()
                out["after_remove"] = table.row_count
            return out

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "telegram_users.json"
            tu.save_users(tu.default_roster(), p)
            r = asyncio.run(scenario(str(p)))
            saved = tu.load_users(p)                       # persisted back to disk?
        self.assertEqual((r["start"], r["after_add"], r["after_remove"]), (1, 2, 1))
        self.assertEqual([u.label for u in saved.users], ["Dad"])


class ApplyToHermesTests(unittest.TestCase):
    def test_apply_writes_routes_preserves_others_leaks_no_secrets(self):
        import dashboard
        import dad_assistant
        import yaml

        r = tu.default_roster()
        r.add(tu.TelegramUser(label="Alice", chat_id="123456789"))
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / "config.yaml").write_text(yaml.safe_dump({"gateway": {"profile_routes": [
                {"name": "disc", "platform": "discord", "chat_id": "1", "profile": "p"},
                {"name": "stale", "platform": "telegram", "chat_id": "7108285456", "profile": "old"},
            ]}}), encoding="utf-8")
            orig = dad_assistant.hermes_home
            dad_assistant.hermes_home = lambda: home
            try:
                n = dashboard.apply_roster_to_hermes(r)
            finally:
                dad_assistant.hermes_home = orig
            cfg = yaml.safe_load((home / "config.yaml").read_text())

        routes = cfg["gateway"]["profile_routes"]
        tele = [x for x in routes if x["platform"] == "telegram"]
        disc = [x for x in routes if x["platform"] == "discord"]
        self.assertEqual(n, 2)                              # Dad + Alice enabled
        self.assertEqual(len(tele), 2)                      # stale telegram route replaced, not doubled
        self.assertEqual(len(disc), 1)                      # non-telegram route preserved
        self.assertTrue(cfg["gateway"]["multiplex_profiles"])
        self.assertNotIn("token", yaml.safe_dump(cfg).lower())


class RouteSafetyTests(unittest.TestCase):
    def test_routes_carry_no_credentials(self):
        # Routes written to Hermes must only ever be chat_id->profile — no tokens/keys.
        r = tu.default_roster()
        r.add(tu.TelegramUser(label="Alice", chat_id="123456789"))
        routes = tu.build_profile_routes(r, [])
        blob = json.dumps(routes)
        self.assertNotIn("token", blob.lower())
        self.assertTrue(all(set(x) == {"name", "platform", "chat_id", "profile"} for x in routes))


if __name__ == "__main__":
    unittest.main(verbosity=2)
