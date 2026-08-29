#!/usr/bin/env python3
"""Send a message FROM 爸菲特 to someone on the roster, unprompted — text first, then voice.

Telegram forbids a bot from opening a conversation, but anyone on the roster has already
messaged it, so announcements ("I have a new feature") can go out from here. The message is
recorded as one of 爸菲特's own turns in that person's memory, so it later remembers having
said it.

    python3 scripts/botffet_push.py --to Dad --file msg.txt
    python3 scripts/botffet_push.py --to Peter --to Dad "短訊息"
    python3 scripts/botffet_push.py --to Peter --voice-file sample.ogg --caption "候選聲音 A"
    python3 scripts/botffet_push.py --to Peter --no-voice --no-record "只傳文字"

Recipients are roster labels or chat ids; nothing outside telegram_users.json can be addressed.
"""

import argparse
import asyncio
import io
import json
import os
import socket
import sys

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS_DIR)

# Same DNS pin as botffet_bot.py — api.telegram.org resolves unreliably on this machine.
_orig_getaddrinfo = socket.getaddrinfo


def _pinned_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if host == 'api.telegram.org':
        return _orig_getaddrinfo('149.154.166.110', port, family, type, proto, flags)
    return _orig_getaddrinfo(host, port, family, type, proto, flags)


socket.getaddrinfo = _pinned_getaddrinfo

from dotenv import load_dotenv           # noqa: E402
from telegram import Bot                 # noqa: E402

import botffet                           # noqa: E402
import botffet_voice                     # noqa: E402

load_dotenv(os.path.join(SCRIPTS_DIR, '.env'))
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
ROSTER_PATH = os.path.join(SCRIPTS_DIR, 'telegram_users.json')
TELEGRAM_CHUNK = 4000


def resolve(names):
    with open(ROSTER_PATH, 'r', encoding='utf-8') as f:
        users = [u for u in json.load(f).get('users', []) if u.get('enabled')]
    out = []
    for name in names:
        hit = next((u for u in users
                    if str(u.get('chat_id')) == str(name)
                    or (u.get('label') or '').lower() == name.lower()), None)
        if not hit:
            raise SystemExit(f'{name!r} is not an enabled roster entry (labels: '
                             f'{", ".join(u.get("label", "?") for u in users)})')
        out.append(hit)
    return out


async def push(recipients, text, *, voice=True, voice_file=None, caption=None, record=True):
    bot = Bot(TOKEN)
    async with bot:
        for user in recipients:
            chat_id = str(user['chat_id'])
            if text:
                for i in range(0, len(text), TELEGRAM_CHUNK):
                    await bot.send_message(chat_id, text[i:i + TELEGRAM_CHUNK])
                if record:
                    botffet.history_append(chat_id, 'assistant', text)
            data = None
            if voice_file:
                with open(voice_file, 'rb') as f:
                    data = f.read()
            elif text and voice and user.get('voice', True):
                try:
                    data = await asyncio.to_thread(botffet_voice.voice_for, text)
                except botffet_voice.VoiceError as exc:
                    print(f'voice skipped for {user.get("label")}: {exc}', file=sys.stderr)
            if data:
                await bot.send_voice(chat_id, io.BytesIO(data), caption=caption)
            print(f"sent to {user.get('label')} ({chat_id}): "
                  f"{len(text or '')} chars{', voice' if data else ''}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--to', action='append', required=True, help='roster label or chat id')
    ap.add_argument('text', nargs='?', help='message text (or use --file)')
    ap.add_argument('--file', help='read the message text from this file')
    ap.add_argument('--no-voice', action='store_true', help='text only')
    ap.add_argument('--voice-file', help='send this .ogg instead of synthesising')
    ap.add_argument('--caption', help='caption under the voice bubble')
    ap.add_argument('--no-record', action='store_true',
                    help="do not enter the message into the recipient's memory")
    args = ap.parse_args()
    if not TOKEN:
        raise SystemExit('TELEGRAM_BOT_TOKEN not set in scripts/.env')
    text = args.text or ''
    if args.file:
        with open(args.file, 'r', encoding='utf-8') as f:
            text = f.read().strip()
    if not text and not args.voice_file:
        raise SystemExit('nothing to send')
    asyncio.run(push(resolve(args.to), text, voice=not args.no_voice,
                     voice_file=args.voice_file, caption=args.caption,
                     record=not args.no_record))


if __name__ == '__main__':
    main()
