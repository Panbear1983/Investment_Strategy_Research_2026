# Sandboxed Dad Assistant TUI Implementation Plan

> **For Hermes:** Implement this plan task-by-task with strict test-first development.

**Goal:** Add a repository-local TUI and CLI that safely configures a Telegram-routed `dad-investment` Hermes profile using Codex OAuth while exposing only the investment CSV read-only and an optional reports directory read-write.

**Architecture:** Keep all credentials in profile-scoped Hermes state under `~/.hermes`; commit only code and an example configuration. Put validation, idempotent route merging, command construction, and subprocess execution in a pure Python module. Add a separate Textual configuration app and link it from the existing dashboard without making `dashboard.py` a configuration god-file.

**Tech Stack:** Python 3.11 standard library, PyYAML already present, Textual 7.5, `unittest`, Hermes CLI, Docker.

---

### Task 1: Add test scaffolding and the non-secret configuration model

**Files:**
- Create: `tests/test_dad_assistant.py`
- Create: `scripts/dad_assistant.py`

**Steps:**
1. Write a failing test for a valid numeric Telegram ID and secure defaults.
2. Run `python3 -m unittest tests.test_dad_assistant -v` and confirm failure because the module does not exist.
3. Implement `DadAssistantConfig` with defaults for profile `dad-investment`, model `gpt-5.6-sol`, `openai_runtime=auto`, low reasoning, priority service tier, CSV-only access, and reports output.
4. Add validation for Telegram IDs, profile slugs, model/runtime values, and paths.
5. Re-run the focused test and confirm it passes.

### Task 2: Generate a least-privilege, idempotent Hermes setup

**Files:**
- Modify: `tests/test_dad_assistant.py`
- Modify: `scripts/dad_assistant.py`

**Steps:**
1. Write failing behavior tests asserting that generated configuration mounts only the CSV as `:ro`, mounts only `dad_reports` as `:rw`, disables Docker networking/env forwarding, keeps `model.openai_runtime=auto`, and exposes only `file`, `code_execution`, and `clarify` on Telegram.
2. Write a failing test that route merging preserves unrelated routes and replaces only the route with the same name/chat ID.
3. Implement pure command builders and route-merging helpers.
4. Implement `apply_configuration()` using `hermes profile create` only when needed, profile-scoped `hermes config set` calls, and a merged default-profile route list.
5. Add dry-run output so the TUI can preview every operation without side effects.
6. Run the focused tests and confirm they pass.

### Task 3: Add safe operational commands

**Files:**
- Modify: `tests/test_dad_assistant.py`
- Modify: `scripts/dad_assistant.py`

**Steps:**
1. Write failing tests for pairing-code validation and command construction.
2. Implement status, Codex auth, Telegram pairing approve/revoke, gateway restart, and sandbox verification entry points.
3. Ensure no command accepts or persists an OAuth token, API key, bot token, password, or cookie.
4. Add a CLI with `status`, `preview`, `apply`, `auth`, `pair`, `revoke`, `restart`, and `verify` subcommands.
5. Run tests and CLI dry-run checks.

### Task 4: Add the Textual Dad Assistant configuration UI

**Files:**
- Create: `scripts/dad_assistant_tui.py`
- Create: `dad-assistant`
- Modify: `scripts/dashboard.py`

**Steps:**
1. Add a testable launch/smoke path for the new app.
2. Build a Textual screen with fields for Telegram ID, owner ID, model, reasoning effort, fast mode, CSV path, report path, and language.
3. Add actions for Save, Preview, Apply, Codex Login, Status, Pair, Revoke, Restart, and Verify.
4. Run interactive OAuth only while the Textual app is suspended so device-code/browser flow remains visible.
5. Add `a` / “Dad Assistant” to the existing dashboard and a standalone `./dad-assistant` launcher.
6. Verify both launchers compile and the app boots under Textual test mode.

### Task 5: Protect secrets and document the workflow

**Files:**
- Create: `.gitignore`
- Create: `scripts/dad_assistant.example.json`
- Create: `docs/dad_assistant_setup.md`
- Create: `dad_reports/.gitkeep`

**Steps:**
1. Ignore `scripts/dad_assistant.local.json`, `scripts/tg_config.json`, and generated reports while retaining `.gitkeep`.
2. Document phone setup, Codex OAuth, subscription/data implications, sandbox boundaries, pairing, restart behavior, verification, and revocation.
3. State explicitly that no bot/OpenAI credential belongs in this repository.

### Task 6: Apply and verify the live profile

**Steps:**
1. Run the CLI preview and inspect every generated command.
2. Apply the configuration for Telegram user `7108285456`.
3. Complete profile-scoped `openai-codex` device login using Peter's browser session.
4. Confirm the separate Alpaca report LaunchAgent and investment research loop remain running or independently scheduled.
5. Restart only `ai.hermes.gateway`; confirm unrelated launchd services are unaffected.
6. Have Dad message `@Panbear_Hermes_bot`, approve the code under `dad-investment`, and verify `/whoami`.
7. Prove the CSV is readable, the reports directory is writable, the rest of the host repository/home is unavailable, Docker networking is disabled, and the reported model is `gpt-5.6-sol` with low reasoning/priority mode.

### Verification commands

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/dad_assistant.py scripts/dad_assistant_tui.py scripts/dashboard.py
./dad-assistant --help
python3 scripts/dad_assistant.py preview
python3 scripts/dad_assistant.py status
```
