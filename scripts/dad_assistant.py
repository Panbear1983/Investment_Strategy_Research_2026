#!/usr/bin/env python3
"""Configure a least-privilege Hermes profile for the investment database.

This module intentionally stores no credentials. OpenAI Codex OAuth and the
Telegram bot token remain in Hermes' profile-scoped credential stores.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

REPO_DIR = Path(__file__).resolve().parents[1]
LOCAL_CONFIG_PATH = REPO_DIR / "scripts" / "dad_assistant.local.json"
DATASET_RELATIVE_PATH = Path("datasets") / "Global_100k_Investment_Database.csv"
REPORTS_RELATIVE_PATH = Path("dad_reports")
ROUTE_NAME = "dad-investment-dm"


@dataclasses.dataclass
class DadAssistantConfig:
    telegram_chat_id: str = ""
    owner_telegram_id: str = "7512954760"
    profile_name: str = "researcher"
    repo_dir: str = str(REPO_DIR)
    bot_username: str = "@Panbear_Hermes_bot"
    model: str = "gpt-5.6-sol"
    reasoning_effort: str = "low"
    fast_mode: bool = True
    openai_runtime: str = "auto"
    language: str = "bilingual"
    reports_enabled: bool = True

    @property
    def repo_path(self) -> Path:
        return Path(self.repo_dir).expanduser().resolve()

    @property
    def dataset_path(self) -> Path:
        return self.repo_path / DATASET_RELATIVE_PATH

    @property
    def reports_path(self) -> Path:
        return self.repo_path / REPORTS_RELATIVE_PATH

    def validate(self, *, require_dataset: bool = True) -> None:
        if not re.fullmatch(r"[1-9][0-9]{4,19}", self.telegram_chat_id):
            raise ValueError("Dad Telegram ID must be a numeric private-chat ID")
        if not re.fullmatch(r"[1-9][0-9]{4,19}", self.owner_telegram_id):
            raise ValueError("Owner Telegram ID must be numeric")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", self.profile_name):
            raise ValueError("Profile name must contain only lowercase letters, numbers, _ or -")
        if self.model != "gpt-5.6-sol":
            raise ValueError("This configuration currently supports gpt-5.6-sol only")
        if self.reasoning_effort not in {"minimal", "low", "medium"}:
            raise ValueError("Reasoning effort must be minimal, low, or medium")
        if self.openai_runtime != "auto":
            raise ValueError("Dad profile must keep model.openai_runtime=auto for Hermes sandboxing")
        if self.language not in {"english", "traditional_chinese", "bilingual"}:
            raise ValueError("Language must be english, traditional_chinese, or bilingual")
        if require_dataset and not self.dataset_path.is_file():
            raise ValueError(f"Dataset not found: {self.dataset_path}")


def save_local_config(cfg: DadAssistantConfig, path: Path = LOCAL_CONFIG_PATH) -> None:
    cfg.validate()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dataclasses.asdict(cfg)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def load_local_config(path: Path = LOCAL_CONFIG_PATH) -> DadAssistantConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    allowed = {field.name for field in dataclasses.fields(DadAssistantConfig)}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"Unknown configuration keys: {', '.join(unknown)}")
    cfg = DadAssistantConfig(**raw)
    cfg.validate()
    return cfg


def build_profile_config_values(cfg: DadAssistantConfig) -> dict[str, str]:
    cfg.validate()
    mounts = [
        f"{cfg.dataset_path}:/workspace/data/{cfg.dataset_path.name}:ro",
    ]
    if cfg.reports_enabled:
        mounts.append(f"{cfg.reports_path}:/workspace/reports:rw")

    values = {
        "model.provider": "openai-codex",
        "model.default": cfg.model,
        "model.openai_runtime": cfg.openai_runtime,
        "agent.reasoning_effort": cfg.reasoning_effort,
        "terminal.backend": "docker",
        "terminal.cwd": "/workspace",
        "terminal.docker_mount_cwd_to_workspace": "false",
        "terminal.docker_network": "false",
        "terminal.docker_forward_env": "[]",
        "terminal.docker_env": "{}",
        "terminal.container_persistent": "false",
        "terminal.docker_persist_across_processes": "false",
        "terminal.docker_volumes": json.dumps(mounts),
        "platform_toolsets.telegram": json.dumps(["file", "code_execution", "clarify"]),
        "gateway.platforms.telegram.enabled": "false",
        "gateway.platforms.telegram.extra.allow_admin_from": json.dumps(
            [cfg.owner_telegram_id]
        ),
        "gateway.platforms.telegram.extra.user_allowed_commands": json.dumps(
            ["new", "stop", "status"]
        ),
        "skills.auto_install": "false",
    }
    if cfg.fast_mode:
        values["agent.service_tier"] = "priority"
    return values


def build_profile_config_mapping(cfg: DadAssistantConfig) -> dict[str, Any]:
    """Return typed YAML values for the profile configuration.

    `hermes config set` coerces booleans and numbers, but intentionally leaves
    list and mapping arguments as strings. Writing these values as typed YAML
    avoids a configuration that looks correct in CLI output but is ignored by
    the runtime.
    """
    cfg.validate()
    mounts = [f"{cfg.dataset_path}:/workspace/data/{cfg.dataset_path.name}:ro"]
    if cfg.reports_enabled:
        mounts.append(f"{cfg.reports_path}:/workspace/reports:rw")
    mapping: dict[str, Any] = {
        "model": {
            "provider": "openai-codex",
            "default": cfg.model,
            "openai_runtime": cfg.openai_runtime,
        },
        "agent": {"reasoning_effort": cfg.reasoning_effort},
        "terminal": {
            "backend": "docker",
            "cwd": "/workspace",
            "docker_mount_cwd_to_workspace": False,
            "docker_network": False,
            "docker_forward_env": [],
            "docker_env": {},
            "container_persistent": False,
            "docker_persist_across_processes": False,
            "docker_volumes": mounts,
        },
        "platform_toolsets": {
            "telegram": ["file", "code_execution", "clarify"],
        },
        "gateway": {
            "platforms": {
                "telegram": {
                    "enabled": False,
                    "extra": {
                        "allow_admin_from": [cfg.owner_telegram_id],
                        "user_allowed_commands": ["new", "stop", "status"],
                    },
                }
            }
        },
        "skills": {"auto_install": False},
    }
    if cfg.fast_mode:
        mapping["agent"]["service_tier"] = "priority"
    return mapping


def dad_route(cfg: DadAssistantConfig) -> dict[str, str]:
    return {
        "name": ROUTE_NAME,
        "platform": "telegram",
        "chat_id": cfg.telegram_chat_id,
        "profile": cfg.profile_name,
    }


def merge_profile_routes(
    existing: Iterable[dict[str, Any]] | None, cfg: DadAssistantConfig
) -> list[dict[str, Any]]:
    replacement = dad_route(cfg)
    merged: list[dict[str, Any]] = []
    replaced = False
    for raw in existing or []:
        route = dict(raw)
        same_name = route.get("name") == ROUTE_NAME
        same_chat = (
            route.get("platform") == "telegram"
            and str(route.get("chat_id", "")) == cfg.telegram_chat_id
        )
        if same_name or same_chat:
            if not replaced:
                merged.append(replacement)
                replaced = True
            continue
        merged.append(route)
    if not replaced:
        merged.append(replacement)
    return merged


def build_pairing_command(cfg: DadAssistantConfig, code: str) -> list[str]:
    normalized = code.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{8}", normalized):
        raise ValueError("Telegram pairing code must be exactly eight letters or numbers")
    return [
        "hermes",
        "-p",
        cfg.profile_name,
        "pairing",
        "approve",
        "telegram",
        normalized,
    ]


def build_revoke_command(cfg: DadAssistantConfig) -> list[str]:
    return [
        "hermes",
        "-p",
        cfg.profile_name,
        "pairing",
        "revoke",
        "telegram",
        cfg.telegram_chat_id,
    ]


def build_gateway_restart_commands() -> list[list[str]]:
    # A full stop/start refreshes a stale launchd service definition; restart
    # alone can keep the old executable path.
    return [["hermes", "gateway", "stop"], ["hermes", "gateway", "start"]]


def build_profile_create_command(cfg: DadAssistantConfig) -> list[str]:
    return [
        "hermes",
        "profile",
        "create",
        cfg.profile_name,
        "--no-skills",
        "--description",
        "Read-only investment database assistant for Dad",
    ]


def hermes_home() -> Path:
    return Path.home() / ".hermes"


def profile_path(cfg: DadAssistantConfig) -> Path:
    return hermes_home() / "profiles" / cfg.profile_name


def read_existing_routes(config_path: Path | None = None) -> list[dict[str, Any]]:
    path = config_path or hermes_home() / "config.yaml"
    if not path.exists():
        return []
    try:
        import yaml

        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise RuntimeError(f"Unable to read Hermes config {path}: {exc}") from exc
    routes = (raw.get("gateway") or {}).get("profile_routes") or []
    if isinstance(routes, str):
        try:
            routes = json.loads(routes)
        except json.JSONDecodeError as exc:
            raise RuntimeError("gateway.profile_routes contains invalid JSON text") from exc
    if not isinstance(routes, list) or not all(isinstance(route, dict) for route in routes):
        raise RuntimeError("gateway.profile_routes must be a list of mappings")
    return routes


def _deep_merge(target: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value
    return target


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        import yaml

        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise RuntimeError(f"Unable to read YAML config {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise RuntimeError(f"YAML config must contain a mapping: {path}")
    return loaded


def _atomic_write_yaml(path: Path, data: dict[str, Any]) -> None:
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".dad-assistant.tmp")
    temp_path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    os.chmod(temp_path, path.stat().st_mode & 0o777 if path.exists() else 0o600)
    os.replace(temp_path, path)


def write_configuration_files(cfg: DadAssistantConfig, home: Path | None = None) -> None:
    """Merge typed profile and gateway configuration without touching credentials."""
    cfg.validate()
    root = home or hermes_home()
    profile_config_path = root / "profiles" / cfg.profile_name / "config.yaml"
    if not profile_config_path.parent.is_dir():
        raise RuntimeError(f"Hermes profile does not exist: {cfg.profile_name}")

    profile_config = _read_yaml_mapping(profile_config_path)
    _deep_merge(profile_config, build_profile_config_mapping(cfg))
    _atomic_write_yaml(profile_config_path, profile_config)

    default_config_path = root / "config.yaml"
    default_config = _read_yaml_mapping(default_config_path)
    gateway = default_config.setdefault("gateway", {})
    if not isinstance(gateway, dict):
        raise RuntimeError("gateway configuration must be a mapping")
    existing_routes = gateway.get("profile_routes") or []
    if isinstance(existing_routes, str):
        try:
            existing_routes = json.loads(existing_routes)
        except json.JSONDecodeError as exc:
            raise RuntimeError("gateway.profile_routes contains invalid JSON text") from exc
    gateway["multiplex_profiles"] = True
    gateway["profile_routes"] = merge_profile_routes(existing_routes, cfg)
    _atomic_write_yaml(default_config_path, default_config)


def setup_preview(cfg: DadAssistantConfig, *, profile_exists: bool) -> str:
    import yaml

    lines: list[str] = []
    if not profile_exists:
        lines.append("$ " + shlex.join(build_profile_create_command(cfg)))
    lines.extend(
        [
            f"WRITE typed YAML: {hermes_home() / 'profiles' / cfg.profile_name / 'config.yaml'}",
            yaml.safe_dump(build_profile_config_mapping(cfg), sort_keys=False).rstrip(),
            f"MERGE route into: {hermes_home() / 'config.yaml'}",
            json.dumps(dad_route(cfg), sort_keys=True),
        ]
    )
    return "\n".join(lines)


def format_commands(commands: Iterable[Sequence[str]]) -> str:
    return "\n".join(shlex.join(list(command)) for command in commands)


def run_commands(
    commands: Iterable[Sequence[str]],
    *,
    check: bool = True,
    capture_output: bool = False,
) -> list[subprocess.CompletedProcess[str]]:
    results: list[subprocess.CompletedProcess[str]] = []
    for command in commands:
        result = subprocess.run(
            list(command),
            text=True,
            capture_output=capture_output,
            check=False,
        )
        results.append(result)
        if check and result.returncode:
            details = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(
                f"Command failed ({result.returncode}): {shlex.join(list(command))}"
                + (f"\n{details}" if details else "")
            )
    return results


def apply_configuration(cfg: DadAssistantConfig) -> list[subprocess.CompletedProcess[str]]:
    cfg.validate()
    if cfg.reports_enabled:
        cfg.reports_path.mkdir(parents=True, exist_ok=True)
    results: list[subprocess.CompletedProcess[str]] = []
    if not profile_path(cfg).is_dir():
        results.extend(run_commands([build_profile_create_command(cfg)]))
    write_configuration_files(cfg)
    return results


def status_commands(cfg: DadAssistantConfig) -> list[list[str]]:
    return [
        ["hermes", "profile", "list"],
        ["hermes", "-p", cfg.profile_name, "auth", "status", "openai-codex"],
        ["hermes", "-p", cfg.profile_name, "pairing", "list"],
        ["hermes", "gateway", "status"],
        ["docker", "version", "--format", "client={{.Client.Version}} server={{.Server.Version}}"],
    ]


def verify_local_prerequisites(cfg: DadAssistantConfig) -> list[str]:
    cfg.validate()
    checks = [f"PASS dataset is present: {cfg.dataset_path}"]
    values = build_profile_config_values(cfg)
    mounts = json.loads(values["terminal.docker_volumes"])
    if any(str(cfg.repo_path) + ":" in mount for mount in mounts):
        raise RuntimeError("Unsafe mount exposes the entire repository")
    if not mounts[0].endswith(":ro"):
        raise RuntimeError("Dataset mount is not read-only")
    checks.append("PASS only the dataset is mounted read-only from the repository")
    if values["terminal.docker_network"] != "false":
        raise RuntimeError("Docker networking is not disabled")
    checks.append("PASS Docker network and environment forwarding are disabled")
    if values["model.openai_runtime"] != "auto":
        raise RuntimeError("Codex app-server runtime would bypass the Hermes tool boundary")
    checks.append("PASS Codex uses the Hermes-managed runtime")
    return checks


def _load_required_config(path: Path) -> DadAssistantConfig:
    if not path.exists():
        raise RuntimeError(f"Local configuration not found: {path}. Run init first.")
    return load_local_config(path)


def _cmd_init(args: argparse.Namespace) -> int:
    cfg = DadAssistantConfig(
        telegram_chat_id=args.telegram_id,
        owner_telegram_id=args.owner_id,
        repo_dir=str(REPO_DIR),
        language=args.language,
    )
    save_local_config(cfg, args.config)
    if cfg.reports_enabled:
        cfg.reports_path.mkdir(parents=True, exist_ok=True)
    print(f"Saved non-secret local configuration to {args.config}")
    return 0


def _cmd_preview(args: argparse.Namespace) -> int:
    cfg = _load_required_config(args.config)
    print(setup_preview(cfg, profile_exists=profile_path(cfg).is_dir()))
    return 0


def _cmd_apply(args: argparse.Namespace) -> int:
    cfg = _load_required_config(args.config)
    apply_configuration(cfg)
    print(f"Configured Hermes profile {cfg.profile_name} for Telegram {cfg.telegram_chat_id}")
    if args.restart:
        run_commands(build_gateway_restart_commands())
        print("Hermes gateway restarted")
    return 0


def _cmd_auth(args: argparse.Namespace) -> int:
    cfg = _load_required_config(args.config)
    command = ["hermes", "-p", cfg.profile_name, "auth", "add", "openai-codex"]
    return subprocess.run(command, check=False).returncode


def _cmd_pair(args: argparse.Namespace) -> int:
    cfg = _load_required_config(args.config)
    run_commands([build_pairing_command(cfg, args.code)])
    return 0


def _cmd_revoke(args: argparse.Namespace) -> int:
    cfg = _load_required_config(args.config)
    run_commands([build_revoke_command(cfg)])
    return 0


def _cmd_restart(_args: argparse.Namespace) -> int:
    run_commands(build_gateway_restart_commands())
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    cfg = _load_required_config(args.config)
    results = run_commands(status_commands(cfg), check=False, capture_output=True)
    for command, result in zip(status_commands(cfg), results):
        print(f"\n$ {shlex.join(command)}")
        print((result.stdout or result.stderr or "(no output)").rstrip())
    return 0 if all(result.returncode == 0 for result in results) else 1


def _cmd_verify(args: argparse.Namespace) -> int:
    cfg = _load_required_config(args.config)
    print("\n".join(verify_local_prerequisites(cfg)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=LOCAL_CONFIG_PATH)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="save non-secret local settings")
    init.add_argument("--telegram-id", required=True)
    init.add_argument("--owner-id", default="7512954760")
    init.add_argument(
        "--language",
        choices=("english", "traditional_chinese", "bilingual"),
        default="bilingual",
    )
    init.set_defaults(func=_cmd_init)

    preview = sub.add_parser("preview", help="print the exact Hermes commands")
    preview.set_defaults(func=_cmd_preview)
    apply = sub.add_parser("apply", help="create or update the sandboxed profile")
    apply.add_argument("--restart", action="store_true")
    apply.set_defaults(func=_cmd_apply)
    auth = sub.add_parser("auth", help="run profile-scoped Codex OAuth login")
    auth.set_defaults(func=_cmd_auth)
    pair = sub.add_parser("pair", help="approve Dad's Telegram pairing code")
    pair.add_argument("code")
    pair.set_defaults(func=_cmd_pair)
    revoke = sub.add_parser("revoke", help="revoke Dad's Telegram access")
    revoke.set_defaults(func=_cmd_revoke)
    restart = sub.add_parser("restart", help="restart only the Hermes gateway")
    restart.set_defaults(func=_cmd_restart)
    status = sub.add_parser("status", help="show profile, auth, pairing, gateway and Docker status")
    status.set_defaults(func=_cmd_status)
    verify = sub.add_parser("verify", help="verify local security invariants")
    verify.set_defaults(func=_cmd_verify)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return args.func(args)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
