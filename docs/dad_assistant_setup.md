# Dad Investment Assistant

This repository includes a least-privilege Textual control panel for a Telegram assistant that can analyze only `datasets/Global_100k_Investment_Database.csv` and save reports only under `dad_reports/`.

## Security boundary

- Hermes profile: `dad-investment`
- Telegram: one private DM route; do not use a group chat
- Model: `gpt-5.6-sol` over profile-scoped OpenAI Codex OAuth
- Runtime: `model.openai_runtime: auto` so Hermes retains tool control
- Dataset mount: read-only at `/workspace/data/Global_100k_Investment_Database.csv`
- Reports mount: read-write at `/workspace/reports`
- Docker network: disabled
- Forwarded environment: empty
- Telegram tools: `file`, `code_execution`, `clarify`
- Administrative commands: unavailable to Dad

The container can see its own base-image filesystem. The only host files it receives are the CSV and `dad_reports/`.

## Never store credentials here

Do not put any Telegram token, OpenAI password, OAuth token, API key, browser cookie, `.env`, or `auth.json` content in this repository. `scripts/tg_config.json` and `scripts/dad_assistant.local.json` are intentionally ignored.

Codex OAuth is stored under the profile's Hermes home, outside this repository.

## Open the TUI

Standalone:

```bash
./dad-assistant
```

From the research dashboard:

```bash
./dash
```

Press `a` for **Dad Assistant**. Closing Dad Assistant returns to the research dashboard.

## CLI workflow

Create the non-secret local settings:

```bash
python3 scripts/dad_assistant.py init --telegram-id 7108285456
```

Review exact commands before changing Hermes:

```bash
python3 scripts/dad_assistant.py preview
python3 scripts/dad_assistant.py verify
```

Apply the sandbox and route:

```bash
python3 scripts/dad_assistant.py apply
```

Authenticate the profile using the owner's ChatGPT/Codex subscription:

```bash
python3 scripts/dad_assistant.py auth
```

This opens OpenAI's device-code flow. Dad never receives the account password or OAuth credential. Requests consume the owner's subscription allowance, and data excerpts sent to the model leave the Mac.

Restart only Hermes Telegram gateway:

```bash
python3 scripts/dad_assistant.py restart
```

This does not restart independent launchd services such as the Alpaca report scheduler or the investment research loop. Any Telegram request actively running at the moment of restart is interrupted, and Telegram is briefly unavailable.

## Phone pairing

1. Dad opens a private chat with `@Panbear_Hermes_bot`.
2. Dad sends `hello`.
3. Dad sends the displayed eight-character pairing code to the owner.
4. Approve it only under `dad-investment`:

```bash
python3 scripts/dad_assistant.py pair ABCD1234
```

Never approve Dad under the unrestricted default profile.

## Verification

```bash
python3 scripts/dad_assistant.py status
python3 scripts/dad_assistant.py verify
```

From Dad's Telegram chat:

```text
/whoami
```

Then ask:

```text
Use Python to summarize the columns in /workspace/data/Global_100k_Investment_Database.csv.
```

A report may be written only to `/workspace/reports`. Attempts to access `/Users/peter`, the rest of this repository, or network services from Docker should fail.

## Revoke access

```bash
python3 scripts/dad_assistant.py revoke
python3 scripts/dad_assistant.py restart
```

The route remains configured but the Telegram identity is no longer paired. Remove the route/profile from Hermes if the assistant is permanently retired.
