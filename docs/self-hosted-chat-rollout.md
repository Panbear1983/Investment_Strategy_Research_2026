# 爸菲特 self-hosted conversation lane — staged rollout

## Goal

Add a self-hosted, OpenAI-compatible cloud-inference provider to the existing
`botffet.answer()` path while preserving the current Telegram bot, local
retrieval boundary, audit trail, and research-loop schedule.

This is an **additive** rollout. The live default remains the existing Claude
path until staging passes and a separate promotion decision is made.

## Non-negotiable boundaries

- Telegram polling remains owned by the existing `botffet_bot.py` process.
- The model never receives a shell, filesystem, Telegram token, raw SQLite
  handle, or unrestricted MCP tools.
- Remote/self-hosted inference receives only a bounded prompt consisting of
  persona, bounded conversation history, and retrieved corpus evidence.
- Commands (`/screen`, `/brief`, etc.) remain deterministic and make zero model
  calls.
- Chat quotas are distinct from research-loop quotas. Provider failures never
  halt or edit the research scheduler.
- Configuration contains endpoint metadata only; credentials stay in environment
  variables or the host secret store and are never committed.

## Token-efficient provider strategy

1. Retrieve a bounded shortlist locally first. No broad corpus is sent remotely.
2. Use the self-hosted provider as the primary synthesis lane when configured.
3. Retry no more than once per provider per question; fail quickly on timeout.
4. Fall back to Claude only when the self-hosted lane is unavailable, times out,
   or produces an unusable response.
5. Preserve provider name, fallback reason, latency, and call count in the audit
   record. That lets us tune the primary model using evidence rather than guesswork.

## Build sequence

### Phase A — provider contract (staging only)

- Add a `ChatProvider` contract and an OpenAI-compatible HTTP provider using
  `BOTFFET_SELF_HOSTED_BASE_URL`, `BOTFFET_SELF_HOSTED_MODEL`, optional
  `BOTFFET_SELF_HOSTED_API_KEY`, `BOTFFET_SELF_HOSTED_TIMEOUT`, and bounded
  `BOTFFET_SELF_HOSTED_MAX_TOKENS` (default 1024; maximum 4096).
- Require HTTPS for a cloud endpoint. Plain HTTP is accepted only on a loopback
  address for local development, never for a remote bearer-token endpoint.
- Add a provider router with modes: `legacy`, `self_hosted_first`, and
  `claude_only`.
- Default to `legacy` whenever no explicit mode is supplied, so existing live
  behavior is byte-for-byte preserved.
- Unit-test successful response parsing, malformed response rejection, timeout,
  unavailable endpoint, and fallback ordering using fake transport only.

### Phase B — retrieval-first shared conversation path

- Build one bounded prompt containing the existing persona, limited history,
  retrieved shortlist, data age, and user question.
- Route self-hosted answers through this path. Claude fallback uses the same
  bounded evidence prompt for parity.
- Keep the existing Claude agentic/MCP path behind `legacy` mode only until its
  quality comparison passes.

### Phase C — adapter parity

- Terminal and Telegram keep calling `botffet.answer()` unchanged.
- Add tests proving a command invokes no provider and both adapters observe the
  same router result.
- Do not start a second Telegram poller in staging.

### Phase D — quality and load gate

- Use a fixed offline question set: country/tier screens, factual company
  questions, multi-turn follow-up, no-match, unsupported/current-data question,
  provider timeout, and quota exhaustion.
- Compare self-hosted answers against stored corpus evidence. Reject fabricated
  tickers, claims, URLs, or current-price assertions.
- Benchmark latency and prompt size. Increase shortlist/history only if evidence
  shows the baseline is insufficient.

### Phase E — promotion (requires explicit approval)

- Configure a real HTTPS endpoint and secret outside the repository.
- Run terminal-only canary first under `self_hosted_first`.
- Review audit output and failure rate.
- Change the live bot mode only during a planned short restart window, with
  `legacy` mode as immediate rollback.

## Current promotion status

Not approved and not attempted. This staging workspace contains no endpoint,
API key, Telegram token, or live bot process.
