#!/usr/bin/env python3
"""Create/configure the shared, STRICTLY read-only Hermes profile that new roster
users route to (telegram_users.DEFAULT_PROFILE).

Strict read-only = dataset mounted :ro, NO writable mounts (reports off), no
network. Writes only the profile's own config.yaml — routing stays in the roster,
and credentials (Codex OAuth) are added separately via `hermes ... auth add`.

    python3 setup_reader_profile.py            create/update the profile
    python3 setup_reader_profile.py --dry-run  print the config, change nothing
"""

from __future__ import annotations

import argparse
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
    verify_local_prerequisites,
)

READER_PROFILE = "investment-research-reader"
OWNER_ID = "7512954760"


def reader_config() -> DadAssistantConfig:
    # telegram_chat_id here is a validation placeholder only — this profile is
    # shared and routed to by the roster, so NO per-chat route is written.
    return DadAssistantConfig(
        telegram_chat_id=OWNER_ID,
        owner_telegram_id=OWNER_ID,
        profile_name=READER_PROFILE,
        reports_enabled=False,  # strict read-only: drops the writable reports mount
    )


def assert_strict_readonly(mapping: dict) -> list[str]:
    mounts = mapping["terminal"]["docker_volumes"]
    if not any(m.endswith(":ro") for m in mounts):
        raise RuntimeError("dataset must be mounted read-only (:ro)")
    writable = [m for m in mounts if m.endswith(":rw")]
    if writable:
        raise RuntimeError(f"strict read-only violated — writable mount(s): {writable}")
    if mapping["terminal"]["docker_network"] is not False:
        raise RuntimeError("network must be disabled")
    return mounts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="print config, write nothing")
    args = ap.parse_args()

    cfg = reader_config()
    cfg.validate()
    mapping = build_profile_config_mapping(cfg)
    mounts = assert_strict_readonly(mapping)

    print(f"profile        : {READER_PROFILE}")
    print(f"model          : {mapping['model']['provider']} / {mapping['model']['default']}")
    print(f"mounts         : {mounts}")
    print(f"docker_network : {mapping['terminal']['docker_network']}")
    print("verify:")
    for line in verify_local_prerequisites(cfg):
        print(f"  {line}")
    print("STRICT READ-ONLY: dataset :ro, no writable mounts, no network")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    pdir = profile_path(cfg)
    if not pdir.is_dir():
        subprocess.run(
            ["hermes", "profile", "create", READER_PROFILE, "--no-skills",
             "--description", "Shared read-only investment database reader"],
            check=True, timeout=90, stdin=subprocess.DEVNULL,
            capture_output=True, text=True,
        )
        print(f"\ncreated Hermes profile: {READER_PROFILE}")
    else:
        print(f"\nprofile already exists: {READER_PROFILE}")

    pcfg_path = hermes_home() / "profiles" / READER_PROFILE / "config.yaml"
    pcfg = _read_yaml_mapping(pcfg_path)
    _deep_merge(pcfg, mapping)
    _atomic_write_yaml(pcfg_path, pcfg)
    print(f"wrote config   : {pcfg_path}")

    print("\nRemaining (interactive / your side):")
    print(f"  ! hermes -p {READER_PROFILE} auth add openai-codex   # Codex login for this profile")
    print("  ! open -a Docker                                     # start the daemon")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
