"""Idempotent daily Taiwan/USA research digest for the verified Telegram group."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

from maintenance_posts import (extract_daily_group_digest_posts, model_digest_prompt,
                               parse_model_digest_summaries, render_model_daily_digest)
from research_state import WorkflowState

SCRIPTS_DIR = Path(__file__).resolve().parent
WORKFLOW_PATH = SCRIPTS_DIR / 'research_workflow.sqlite3'
TG_CONFIG_PATH = SCRIPTS_DIR / 'tg_config.json'
GROUP_RECIPIENT = {'label': '財經推播Agent', 'chat_id': '-1003863698623'}
EXPECTED_BOT_ID = 8601109385
EXPECTED_GROUP_TITLE = '財經推播Agent'


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


def run_daily_group_digest(workflow, digest_date, run_model, telegram_request):
    """Deliver one complete daily digest, or persist a no-op/failure without a partial send."""
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
        return {'status': 'sent', 'count': len(posts), 'message_id': message_id,
                'payload_sha256': claim['payload_sha256']}
    except Exception as exc:
        workflow.record_digest_failed(digest_date, GROUP_RECIPIENT, type(exc).__name__, str(exc))
        return {'status': 'failed', 'count': len(posts), 'error_class': type(exc).__name__}


def _load_token():
    config = json.loads(TG_CONFIG_PATH.read_text(encoding='utf-8'))
    token = config.get('bot_token')
    if not token:
        raise RuntimeError('telegram bot token is not configured')
    return token


def _telegram_request(token):
    def request(method, payload):
        data = json.dumps(payload).encode('utf-8')
        url = f'https://api.telegram.org/bot{token}/{method}'
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    url, data=data, headers={'Content-Type': 'application/json'}), timeout=20) as response:
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
    args = parser.parse_args()
    workflow = WorkflowState(str(WORKFLOW_PATH))
    if not args.send:
        posts = extract_daily_group_digest_posts(workflow, args.date)
        print(json.dumps({'status': 'dry_run', 'date': args.date, 'count': len(posts),
                          'tickers': [post['ticker'] for post in posts]}, ensure_ascii=False))
        return
    result = run_daily_group_digest(workflow, args.date, _run_claude,
                                    _telegram_request(_load_token()))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
