import json
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from dad_assistant import (  # noqa: E402
    DadAssistantConfig,
    build_gateway_restart_commands,
    build_pairing_command,
    build_profile_config_mapping,
    build_profile_config_values,
    build_revoke_command,
    load_local_config,
    merge_profile_routes,
    save_local_config,
    write_configuration_files,
)


class DadAssistantConfigTests(unittest.TestCase):
    def make_config(self, root: Path) -> DadAssistantConfig:
        dataset = root / "datasets" / "Global_100k_Investment_Database.csv"
        dataset.parent.mkdir(parents=True)
        dataset.write_text("header\n", encoding="utf-8")
        return DadAssistantConfig(
            telegram_chat_id="7108285456",
            owner_telegram_id="7512954760",
            repo_dir=str(root),
        )

    def test_secure_defaults_validate(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.make_config(Path(td))

            cfg.validate()

            self.assertEqual(cfg.profile_name, "researcher")
            self.assertEqual(cfg.model, "gpt-5.6-sol")
            self.assertEqual(cfg.reasoning_effort, "low")
            self.assertTrue(cfg.fast_mode)
            self.assertEqual(cfg.openai_runtime, "auto")
            self.assertEqual(cfg.language, "bilingual")

    def test_telegram_id_must_be_numeric(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.make_config(Path(td))
            cfg.telegram_chat_id = "@dad"

            with self.assertRaisesRegex(ValueError, "numeric"):
                cfg.validate()

    def test_profile_values_mount_only_csv_read_only_and_reports_read_write(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            cfg = self.make_config(root)

            values = build_profile_config_values(cfg)
            mounts = json.loads(values["terminal.docker_volumes"])

            self.assertEqual(
                mounts,
                [
                    f"{root / 'datasets' / 'Global_100k_Investment_Database.csv'}:/workspace/data/Global_100k_Investment_Database.csv:ro",
                    f"{root / 'dad_reports'}:/workspace/reports:rw",
                ],
            )
            self.assertNotIn(str(root) + ":", "\n".join(mounts))
            self.assertEqual(values["terminal.backend"], "docker")
            self.assertEqual(values["terminal.cwd"], "/workspace")
            self.assertEqual(values["terminal.docker_network"], "false")
            self.assertEqual(values["terminal.docker_forward_env"], "[]")

    def test_profile_values_use_codex_default_runtime_and_restricted_tools(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.make_config(Path(td))

            values = build_profile_config_values(cfg)

            self.assertEqual(values["model.provider"], "openai-codex")
            self.assertEqual(values["model.default"], "gpt-5.6-sol")
            self.assertEqual(values["model.openai_runtime"], "auto")
            self.assertEqual(values["agent.reasoning_effort"], "low")
            self.assertEqual(values["agent.service_tier"], "priority")
            self.assertEqual(
                json.loads(values["platform_toolsets.telegram"]),
                ["file", "code_execution", "clarify"],
            )
            self.assertEqual(values["gateway.platforms.telegram.enabled"], "false")

    def test_route_merge_preserves_unrelated_routes_and_replaces_dad_route(self):
        cfg = DadAssistantConfig(
            telegram_chat_id="7108285456",
            owner_telegram_id="7512954760",
            repo_dir="/tmp/repo",
        )
        existing = [
            {"name": "team", "platform": "discord", "guild_id": "42", "profile": "work"},
            {"name": "dad-investment-dm", "platform": "telegram", "chat_id": "old", "profile": "old"},
        ]

        merged = merge_profile_routes(existing, cfg)

        self.assertEqual(merged[0], existing[0])
        self.assertEqual(
            merged[1],
            {
                "name": "dad-investment-dm",
                "platform": "telegram",
                "chat_id": "7108285456",
                "profile": "researcher",
            },
        )
        self.assertEqual(len(merged), 2)

    def test_pairing_code_is_validated_before_command_construction(self):
        cfg = DadAssistantConfig(
            telegram_chat_id="7108285456",
            owner_telegram_id="7512954760",
            repo_dir="/tmp/repo",
        )

        self.assertEqual(
            build_pairing_command(cfg, "AB12CD34"),
            ["hermes", "-p", "researcher", "pairing", "approve", "telegram", "AB12CD34"],
        )
        with self.assertRaisesRegex(ValueError, "pairing code"):
            build_pairing_command(cfg, "bad code; rm -rf /")

    def test_local_config_round_trip_contains_no_credentials(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = self.make_config(root)
            path = root / "dad_assistant.local.json"

            save_local_config(cfg, path)
            raw = path.read_text(encoding="utf-8")
            restored = load_local_config(path)

            self.assertEqual(restored.telegram_chat_id, "7108285456")
            self.assertNotIn("token", raw.lower())
            self.assertNotIn("password", raw.lower())
            self.assertNotIn("api_key", raw.lower())
            self.assertNotIn("oauth", raw.lower())

    def test_profile_mapping_uses_native_list_and_mapping_types(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.make_config(Path(td))

            mapping = build_profile_config_mapping(cfg)

            self.assertIsInstance(mapping["terminal"]["docker_volumes"], list)
            self.assertIsInstance(mapping["terminal"]["docker_env"], dict)
            self.assertIsInstance(mapping["terminal"]["docker_forward_env"], list)
            self.assertIsInstance(mapping["platform_toolsets"]["telegram"], list)
            self.assertIsInstance(
                mapping["gateway"]["platforms"]["telegram"]["extra"]["allow_admin_from"],
                list,
            )

    def test_typed_yaml_write_preserves_unrelated_routes_and_settings(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = self.make_config(root / "repo")
            profile_config = root / "hermes" / "profiles" / "researcher" / "config.yaml"
            default_config = root / "hermes" / "config.yaml"
            profile_config.parent.mkdir(parents=True)
            default_config.parent.mkdir(parents=True, exist_ok=True)
            profile_config.write_text("display:\n  compact: true\n", encoding="utf-8")
            default_config.write_text(
                "gateway:\n"
                "  profile_routes:\n"
                "    - name: work\n"
                "      platform: slack\n"
                "      team_id: T1\n"
                "      profile: work\n",
                encoding="utf-8",
            )

            write_configuration_files(cfg, root / "hermes")

            profile = yaml.safe_load(profile_config.read_text(encoding="utf-8"))
            gateway = yaml.safe_load(default_config.read_text(encoding="utf-8"))
            self.assertTrue(profile["display"]["compact"])
            self.assertIsInstance(profile["terminal"]["docker_volumes"], list)
            self.assertFalse(profile["terminal"]["docker_network"])
            self.assertEqual(profile["platform_toolsets"]["telegram"], ["file", "code_execution", "clarify"])
            self.assertEqual(gateway["gateway"]["profile_routes"][0]["name"], "work")
            self.assertEqual(gateway["gateway"]["profile_routes"][1]["chat_id"], "7108285456")
            self.assertTrue(gateway["gateway"]["multiplex_profiles"])

    def test_typed_yaml_write_migrates_legacy_string_route(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = self.make_config(root / "repo")
            profile_config = root / "hermes" / "profiles" / "researcher" / "config.yaml"
            default_config = root / "hermes" / "config.yaml"
            profile_config.parent.mkdir(parents=True)
            profile_config.write_text("{}\n", encoding="utf-8")
            default_config.write_text(
                "gateway:\n  profile_routes: '[{\"name\":\"old\",\"platform\":\"slack\",\"profile\":\"work\"}]'\n",
                encoding="utf-8",
            )

            write_configuration_files(cfg, root / "hermes")

            gateway = yaml.safe_load(default_config.read_text(encoding="utf-8"))
            self.assertIsInstance(gateway["gateway"]["profile_routes"], list)
            self.assertEqual(gateway["gateway"]["profile_routes"][0]["name"], "old")

    def test_revoke_and_restart_commands_are_argument_arrays(self):
        cfg = DadAssistantConfig(
            telegram_chat_id="7108285456",
            owner_telegram_id="7512954760",
            repo_dir="/tmp/repo",
        )

        self.assertEqual(
            build_revoke_command(cfg),
            [
                "hermes",
                "-p",
                "researcher",
                "pairing",
                "revoke",
                "telegram",
                "7108285456",
            ],
        )
        self.assertEqual(
            build_gateway_restart_commands(),
            [["hermes", "gateway", "stop"], ["hermes", "gateway", "start"]],
        )


if __name__ == "__main__":
    unittest.main()
