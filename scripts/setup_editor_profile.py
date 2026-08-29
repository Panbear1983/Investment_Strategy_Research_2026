#!/usr/bin/env python3
"""Create/configure the 'investment-research-editor' Hermes profile for the roster's
read-write tier.

Safety model: this profile mounts a COPY of the dataset :rw (under staging/), so a
read-write user edits an ISOLATED sandbox copy — never the live 100k database the
research loop reads and rewrites. Their edits do not propagate back to the shared DB.
(If you want edits that persist to the real database, that is a separate, riskier
live-write design — pass --live to mount the real file instead.)

    python3 setup_editor_profile.py --dry-run   print config, change nothing
    python3 setup_editor_profile.py             create with an isolated dataset copy
    python3 setup_editor_profile.py --live      DANGER: mount the live dataset :rw
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dad_assistant import (
    DadAssistantConfig,
    _atomic_write_yaml,
    _deep_merge,
    _read_yaml_mapping,
    build_profile_config_mapping,
    hermes_home,
    profile_path,
)
import telegram_users as tu

EDITOR_PROFILE = tu.EDITOR_PROFILE  # "investment-research-editor"
OWNER_ID = "7512954760"
REPO = Path(__file__).resolve().parents[1]
STAGING = REPO / "staging"
COPY_PATH = STAGING / "investment_editor_copy.csv"


def build_mapping(live: bool):
    # telegram_chat_id is a validation placeholder only (no per-chat route written here).
    cfg = DadAssistantConfig(telegram_chat_id=OWNER_ID, owner_telegram_id=OWNER_ID,
                             profile_name=EDITOR_PROFILE, reports_enabled=False)
    cfg.validate()
    mapping = build_profile_config_mapping(cfg)  # gives a :ro live-dataset mount…
    target = cfg.dataset_path if live else COPY_PATH  # …which we replace with a :rw mount
    mapping["terminal"]["docker_volumes"] = [
        f"{target}:/workspace/data/{Path(target).name}:rw"
    ]
    # safety asserts
    mounts = mapping["terminal"]["docker_volumes"]
    assert mounts[0].endswith(":rw"), "editor mount must be read-write"
    assert mapping["terminal"]["docker_network"] is False, "network must stay off"
    if not live:
        assert str(cfg.dataset_path) not in mounts[0], "isolated copy must not mount the live file"
    return cfg, mapping, target


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="print config, write nothing")
    ap.add_argument("--live", action="store_true",
                    help="DANGER: mount the live dataset :rw instead of an isolated copy")
    args = ap.parse_args()

    cfg, mapping, target = build_mapping(args.live)
    print(f"profile        : {EDITOR_PROFILE}")
    print(f"model          : {mapping['model']['provider']} / {mapping['model']['default']}")
    print(f"write target   : {'LIVE DATABASE (risky)' if args.live else f'isolated copy {target}'}")
    print(f"mounts         : {mapping['terminal']['docker_volumes']}")
    print(f"docker_network : {mapping['terminal']['docker_network']}")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    if not args.live:
        STAGING.mkdir(parents=True, exist_ok=True)
        if not COPY_PATH.exists():
            shutil.copy2(cfg.dataset_path, COPY_PATH)
            print(f"copied dataset -> {COPY_PATH} ({COPY_PATH.stat().st_size // 1024 // 1024} MB)")

    if not profile_path(cfg).is_dir():
        subprocess.run(
            ["hermes", "profile", "create", EDITOR_PROFILE, "--no-skills", "--description",
             "Read-write investment editor" + ("" if args.live else " (isolated dataset copy)")],
            check=True, timeout=90, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        )
        print(f"created Hermes profile: {EDITOR_PROFILE}")

    pcfg_path = hermes_home() / "profiles" / EDITOR_PROFILE / "config.yaml"
    pcfg = _read_yaml_mapping(pcfg_path)
    _deep_merge(pcfg, mapping)
    _atomic_write_yaml(pcfg_path, pcfg)
    print(f"wrote config   : {pcfg_path}")
    print(f"\nRemaining: ! hermes -p {EDITOR_PROFILE} auth add openai-codex   (Codex login)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
