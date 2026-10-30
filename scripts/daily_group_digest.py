"""Idempotent daily Taiwan/USA research digest for the verified Telegram group.

Since 2026-10-27 the text is followed by a voice bubble (Peter: "need you to add the voice
message to this kind of push for all future pushes"), spoken in 爸菲特's voice from
botffet_voice.py. Voice is strictly after the text and strictly best-effort: it starts only
once the text is sent and recorded, and nothing it does can change the run's status, the
delivery ledger or the exit code. Switch it off with --no-voice or ISR_DIGEST_VOICE=0.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

from maintenance_posts import (extract_daily_group_digest_posts, model_digest_prompt,
                               parse_model_digest_summaries, render_model_daily_digest)
from research_state import WorkflowState

SCRIPTS_DIR = Path(__file__).resolve().parent
WORKFLOW_PATH = SCRIPTS_DIR / 'research_workflow.sqlite3'
TG_CONFIG_PATH = SCRIPTS_DIR / 'tg_config.json'
DIGEST_LOG_PATH = SCRIPTS_DIR / 'daily_group_digest.log'
GROUP_RECIPIENT = {'label': '財經推播Agent', 'chat_id': '-1003863698623'}
EXPECTED_BOT_ID = 8601109385
EXPECTED_GROUP_TITLE = '財經推播Agent'

# Spoken-digest length cap, so the bubble stays under two minutes. Measured on the real
# 2026-10-26 digest in the Edge voice (-5%): 409 characters took 72.9 s, about 5.6 a second,
# so 640 characters is about 1:55. The whole text sits directly above the bubble, so a cut
# tail loses nothing.
VOICE_MAX_CHARS = 640
VOICE_TRUNCATED_NOTE = '其餘請看文字。'
VOICE_ENV = 'ISR_DIGEST_VOICE'


def _today_taipei():
    return dt.datetime.now(ZoneInfo('Asia/Taipei')).date().isoformat()


def _render_group_digest(posts, summaries, digest_date):
    rendered = render_model_daily_digest(posts, summaries, digest_date)
    lines = rendered.splitlines()
    body = lines[2:] if len(lines) >= 2 else lines
    return '\n'.join([
        f'📊 每日投資研究更新｜{digest_date}',
        f'台灣／美國新增與維護更新 {len(posts)} 家（新增公司優先）',
        *body,
    ])


def _preflight(telegram_request):
    identity = telegram_request('getMe', {})
    bot = identity.get('result') or {}
    if not identity.get('ok') or bot.get('id') != EXPECTED_BOT_ID:
        raise RuntimeError('telegram_bot_identity_mismatch')
    chat = telegram_request('getChat', {'chat_id': GROUP_RECIPIENT['chat_id']})
    result = chat.get('result') or {}
    if (not chat.get('ok') or str(result.get('id')) != GROUP_RECIPIENT['chat_id']
            or result.get('type') not in ('group', 'supergroup')
            or result.get('title') != EXPECTED_GROUP_TITLE):
        raise RuntimeError('telegram_group_mismatch')


def run_daily_group_digest(workflow, digest_date, run_model, telegram_request, *,
                           voice=False, render_voice=None, log_path=None):
    """Deliver one complete daily digest, or persist a no-op/failure without a partial send.

    With `voice` on, a spoken version follows the text as a reply to it. That step runs only
    after the text is sent and recorded, and whatever it does, the result stays 'sent'.
    Off by default so a caller (or a test) never synthesises or uploads audio by accident.
    """
    posts = extract_daily_group_digest_posts(workflow, digest_date)
    if not posts:
        workflow.record_digest_no_updates(digest_date, GROUP_RECIPIENT)
        return {'status': 'skipped_no_updates', 'count': 0}
    payload = {'date': digest_date, 'recipient': GROUP_RECIPIENT, 'posts': posts}
    claim = workflow.claim_digest_delivery(digest_date, GROUP_RECIPIENT, payload)
    if not claim['claimed']:
        return {'status': f"already_{claim['status']}", 'count': len(posts),
                'payload_sha256': claim['payload_sha256']}
    try:
        _preflight(telegram_request)
        summaries = parse_model_digest_summaries(posts, run_model(model_digest_prompt(posts)))
        expected = {post['ticker'] for post in posts}
        if set(summaries) != expected:
            raise RuntimeError('model_summary_incomplete')
        message = _render_group_digest(posts, summaries, digest_date)
        if len(message) > 1800:
            raise RuntimeError('digest_message_too_long')
        sent = telegram_request('sendMessage', {
            'chat_id': GROUP_RECIPIENT['chat_id'],
            'text': message,
            'disable_web_page_preview': True,
        })
        if not sent.get('ok'):
            raise RuntimeError('telegram_send_failed')
        message_id = (sent.get('result') or {}).get('message_id')
        if message_id is None:
            raise RuntimeError('telegram_message_id_missing')
        workflow.record_digest_sent(digest_date, GROUP_RECIPIENT, message_id)
    except Exception as exc:
        workflow.record_digest_failed(digest_date, GROUP_RECIPIENT, type(exc).__name__, str(exc))
        return {'status': 'failed', 'count': len(posts), 'error_class': type(exc).__name__}
    result = {'status': 'sent', 'count': len(posts), 'message_id': message_id,
              'payload_sha256': claim['payload_sha256']}
    if voice:
        result.update(_send_digest_voice(telegram_request, posts, summaries, digest_date,
                                         message_id, render_voice, log_path))
    return result


# --- voice bubble ----------------------------------------------------------------------------

_TICKER_IN_PARENS = re.compile(r'\s*[(（][A-Za-z0-9.\-^=]{1,15}[)）]')
_CLAUSE_END = '。；，！？!?;,'
_SENTENCE_END = '。！？!?'


def _spoken_summary(summary):
    """A summary the model wrote for reading, made fit for hearing. A summary that was cut
    for length ends in '…' mid-word; back it up to the last clause so the voice does not
    stop on half a word."""
    text = ' '.join(str(summary or '').split()).replace('｜', '，')
    if text.endswith('…'):
        text = text[:-1]
        cut = max(text.rfind(ch) for ch in _CLAUSE_END)
        if cut > 0:
            text = text[:cut]
    text = text.rstrip(_CLAUSE_END + ' ')
    return f'{text}。' if text else ''


def speakable_digest(posts, summaries, digest_date, max_chars=VOICE_MAX_CHARS):
    """The digest as something to listen to, not the message read aloud.

    "每日投資研究更新，10月26日，5家。" then "第N家，<name>。<summary>" per company. Tickers
    are dropped (the listener has the text for them), and emoji, the ｜ separator and list
    numbering never enter. Longer than `max_chars`, it stops after the last whole company
    that fits and says 其餘請看文字.
    """
    try:
        day = dt.date.fromisoformat(digest_date)
        when = f'{day.month}月{day.day}日'
    except ValueError:
        when = str(digest_date)
    parts = [f'每日投資研究更新，{when}，{len(posts)}家。']
    for index, post in enumerate(posts, 1):
        summary = _spoken_summary(summaries.get(post['ticker']))
        if not summary:
            continue
        name = _TICKER_IN_PARENS.sub('', str(post.get('company_name') or '')).strip()
        name = name or str(post.get('ticker') or '')
        parts.append(f'第{index}家，{name}。{summary}')
    text = '\n'.join(parts)
    text = _TICKER_IN_PARENS.sub('', text)
    try:
        import botffet_voice
        text = botffet_voice.speakable(text, len(text) + 1)    # clean only; the cut is ours
    except Exception:  # noqa: BLE001 - cleaning is a nicety; the builder above has no emoji
        pass
    return _cap_spoken(text, max_chars)


def _cap_spoken(text, max_chars):
    """Cut to `max_chars`, ending with 其餘請看文字. Prefers to stop after a whole company
    (each is one line), so the voice never announces 第N家 and then skips its summary; only
    if even the first company does not fit does it fall back to the last full sentence."""
    if len(text) <= max_chars:
        return text
    room = max_chars - len(VOICE_TRUNCATED_NOTE) - 1
    head = text[:room]
    line_cut = head.rfind('\n')
    if line_cut > 0 and '\n' in text[:line_cut]:            # header + at least one company
        head = text[:line_cut]
    else:
        cut = max((i + 1 for i, ch in enumerate(head) if ch in _SENTENCE_END), default=0)
        head = text[:cut] if cut else head
    return head.rstrip() + '\n' + VOICE_TRUNCATED_NOTE


def _render_voice(spoken, report):
    """爸菲特's voice (botffet_voice.py) as Opus-in-Ogg bytes. Imported here, not at the top,
    so a broken voice module can never stop the text digest from loading."""
    import botffet_voice
    cfg = botffet_voice.settings()
    cfg['max_chars'] = max(cfg['max_chars'], len(spoken))     # already capped by us
    return botffet_voice.voice_for(spoken, cfg, report)


def _log_event(record, log_path=None):
    """One JSON line in daily_group_digest.log. Never raises: a log that cannot be written
    must not turn a delivered digest into an error."""
    line = json.dumps(record, ensure_ascii=False)
    try:
        with open(log_path or DIGEST_LOG_PATH, 'a', encoding='utf-8') as handle:
            handle.write(line + '\n')
    except OSError:
        print(line, file=sys.stderr)


def _send_digest_voice(telegram_request, posts, summaries, digest_date, reply_to,
                       render_voice=None, log_path=None):
    """Speak the digest and post it as a reply under its text. Best-effort by contract:
    every failure (synthesis, ffmpeg, network, Telegram) becomes one voice_failed log line."""
    record = {'date': digest_date, 'reply_to': reply_to,
              'at': dt.datetime.now(ZoneInfo('Asia/Taipei')).isoformat(timespec='seconds')}
    try:
        spoken = speakable_digest(posts, summaries, digest_date)
        record['chars'] = len(spoken)
        report = {}
        try:
            data = (render_voice or _render_voice)(spoken, report)
        finally:
            record.update({key: report[key] for key in ('engine', 'voice', 'fallback', 'attempts')
                           if key in report})
        if not data:
            raise RuntimeError('voice_empty')
        sent = telegram_request('sendVoice', {
            'chat_id': GROUP_RECIPIENT['chat_id'],
            'reply_to_message_id': reply_to,
            'voice': bytes(data),
        })
        if not sent.get('ok'):
            raise RuntimeError('telegram_send_voice_failed: '
                               + str(sent.get('description') or sent.get('error_class') or ''))
        voice_id = (sent.get('result') or {}).get('message_id')
        _log_event({'status': 'voice_sent', 'message_id': voice_id, 'bytes': len(data), **record},
                   log_path)
        return {'voice': 'sent', 'voice_message_id': voice_id}
    except Exception as exc:  # noqa: BLE001 - voice must never cost the text
        _log_event({'status': 'voice_failed', 'error_class': type(exc).__name__,
                    'error': str(exc)[:300], **record}, log_path)
        return {'voice': 'failed'}


def voice_enabled(no_voice_flag=False, environ=None):
    """On unless --no-voice was given or ISR_DIGEST_VOICE is 0/false/no/off."""
    if no_voice_flag:
        return False
    value = str((os.environ if environ is None else environ).get(VOICE_ENV, '')).strip().lower()
    return value not in ('0', 'false', 'no', 'off')


def _load_token():
    config = json.loads(TG_CONFIG_PATH.read_text(encoding='utf-8'))
    token = config.get('bot_token')
    if not token:
        raise RuntimeError('telegram bot token is not configured')
    return token


def _multipart(payload):
    """multipart/form-data body for an upload (sendVoice); bytes values become the file."""
    boundary = uuid.uuid4().hex
    chunks = []
    for name, value in payload.items():
        if isinstance(value, (bytes, bytearray)):
            chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                           f'filename="digest.ogg"\r\nContent-Type: audio/ogg\r\n\r\n').encode('utf-8')
                          + bytes(value) + b'\r\n')
        else:
            text = value if isinstance(value, str) else json.dumps(value)
            chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                           f'\r\n\r\n{text}\r\n').encode('utf-8'))
    chunks.append(f'--{boundary}--\r\n'.encode('utf-8'))
    return b''.join(chunks), f'multipart/form-data; boundary={boundary}'


def _telegram_request(token):
    def request(method, payload):
        if any(isinstance(value, (bytes, bytearray)) for value in payload.values()):
            # An upload: the voice bubble. The upload, not the call, is the slow leg on this
            # Mac's link (botffet_voice notes, 2026-09-03), hence the longer timeout.
            data, content_type = _multipart(payload)
            timeout = 120
        else:
            data, content_type = json.dumps(payload).encode('utf-8'), 'application/json'
            timeout = 20
        url = f'https://api.telegram.org/bot{token}/{method}'
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    url, data=data, headers={'Content-Type': content_type}), timeout=timeout) as response:
                return json.loads(response.read().decode('utf-8'))
        except (urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
            return {'ok': False, 'error_class': type(exc).__name__}
    return request


def _run_claude(prompt):
    completed = subprocess.run(['claude', '-p', prompt, '--model', 'sonnet'],
                               capture_output=True, text=True, timeout=240)
    if completed.returncode:
        raise RuntimeError(f'claude_exit_{completed.returncode}')
    return completed.stdout


def main():
    parser = argparse.ArgumentParser(description='Send one guarded daily investment group digest.')
    parser.add_argument('--date', default=_today_taipei(), help='Asia/Taipei YYYY-MM-DD date')
    parser.add_argument('--send', action='store_true', help='allow Claude and Telegram side effects')
    parser.add_argument('--no-voice', action='store_true',
                        help=f'text only, no voice bubble (or set {VOICE_ENV}=0)')
    args = parser.parse_args()
    workflow = WorkflowState(str(WORKFLOW_PATH))
    if not args.send:
        posts = extract_daily_group_digest_posts(workflow, args.date)
        print(json.dumps({'status': 'dry_run', 'date': args.date, 'count': len(posts),
                          'tickers': [post['ticker'] for post in posts]}, ensure_ascii=False))
        return
    result = run_daily_group_digest(workflow, args.date, _run_claude,
                                    _telegram_request(_load_token()),
                                    voice=voice_enabled(args.no_voice))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
