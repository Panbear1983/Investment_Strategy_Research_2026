#!/usr/bin/env python3
"""爸菲特 / Wanna Botffet — Telegram front door to the research corpus.

Long-polls bot 8601109385 (@Hermes_Investment_Strategy_bot) and hands every message to
botffet.answer(), the same function the CLI uses. No second brain: routing, retrieval,
synthesis, quota and audit all live in botffet.py, so Telegram and CLI answers cannot drift.

Run:  python3 scripts/botffet_bot.py
Only ONE process may poll a given bot token — two pollers cause a 409 and one side silently
starves. The getMe preflight below asserts we are the bot we think we are before polling.
"""

import asyncio
import io
import json
import logging
import os
import socket
import sys
import time
from pathlib import Path

# DNS pin for api.telegram.org, matching the --resolve workaround the rest of the repo uses.
_orig_getaddrinfo = socket.getaddrinfo


def _pinned_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if host == 'api.telegram.org':
        return _orig_getaddrinfo('149.154.166.110', port, family, type, proto, flags)
    return _orig_getaddrinfo(host, port, family, type, proto, flags)


socket.getaddrinfo = _pinned_getaddrinfo

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
import botffet

try:
    import video_intel
except Exception:            # the bot must still answer questions if the helper is unavailable
    video_intel = None

try:
    import botffet_voice
except Exception:            # likewise: no voice is a degraded reply, not a dead bot
    botffet_voice = None

load_dotenv(SCRIPTS_DIR / '.env')
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
ROSTER_PATH = SCRIPTS_DIR / 'telegram_users.json'

EXPECTED_BOT_ID = 8601109385          # @Hermes_Investment_Strategy_bot
ANSWER_TIMEOUT = 200                  # > botffet.CLAUDE_TIMEOUT so the provider errors first
# A video is a different shape of work: fetching a transcript then one model call per ~12k
# characters. An hour-long video is roughly six calls, which comfortably exceeds ANSWER_TIMEOUT.
# _in_flight already stops anyone queueing a second one behind it.
# A video with subtitles is seconds of work. One without them is transcribed locally on this
# Mac at roughly four times real time, so a two-hour video is around half an hour. This bound
# exists to stop a hung process, not to cut a long transcription short.
VIDEO_TIMEOUT = 3600
TELEGRAM_CHUNK = 4000                 # Telegram hard-caps a message at 4096

logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=logging.INFO)
log = logging.getLogger('botffet_bot')

# httpx logs every request URL at INFO, and a Telegram request URL embeds the bot token in its
# path — that wrote the live credential into this log 116,154 times over two weeks. WARNING
# keeps genuine transport failures visible without the URLs.
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)

# chat_id -> True while a turn is in flight. Without this one user can queue several slow
# questions and monopolise the worker pool.
_in_flight: set[str] = set()


class Roster:
    """Allowed-list re-read from disk when the file changes.

    The previous implementation snapshotted the roster at import, so disabling a user in the
    dashboard left them working until someone restarted the process — an access-control list
    that does not revoke is not an access-control list.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._mtime = None
        self._users = {}

    def _reload_if_changed(self):
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self._users = {}
            return
        if mtime == self._mtime:
            return
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            log.warning('roster unreadable; denying all until it parses')
            self._users = {}
            return
        self._users = {str(u['chat_id']): u for u in data.get('users', [])
                       if u.get('enabled') and u.get('chat_id')}
        self._mtime = mtime
        log.info('roster loaded: %d enabled user(s)', len(self._users))

    def get(self, chat_id):
        self._reload_if_changed()
        return self._users.get(str(chat_id))


roster = Roster(ROSTER_PATH)


def _video_ack(chat_id):
    """Sent the moment a link arrives, so the wait does not look like a dead bot.

    Says what is left of today's allowance rather than an estimated call count: the allowance is
    the number that actually decides whether the next link will be processed.
    """
    try:
        cap = int(botffet.DEFAULT_VIDEO_CAP)
        used = botffet.quota_used(botffet._video_quota_key(chat_id))
        remaining = max(0, cap - used)
        tail = f'（今天還可以處理 {remaining} 部影片）'
    except Exception:
        tail = ''
    return ('🎬 收到連結，正在抓取逐字稿並整理重點，完成後會再傳給你。\n'
            '如果這部影片沒有字幕，我會改用本機語音辨識，時間大約是影片長度的四分之一，'
            '例如 30 分鐘的影片約需 8 分鐘。' + tail)


def _chunks(text, size=TELEGRAM_CHUNK):
    for i in range(0, len(text), size):
        yield text[i:i + size]


async def _speak(message, text, user):
    """Follow a text reply with the same words as a voice bubble (see botffet_voice.py).

    Always AFTER the text, never instead of it, and never allowed to fail the reply: the
    synthesis is a network call to Edge plus an ffmpeg transcode, so it runs off the event
    loop and any error is logged and dropped. `"voice": false` on a roster entry opts that
    person out.
    """
    if not botffet_voice or not user.get('voice', True):
        return
    try:
        data = await asyncio.to_thread(botffet_voice.voice_for, text)
        if data:
            await message.reply_voice(voice=io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        log.warning('voice skipped for chat_id=%s: %s', message.chat_id, str(exc)[:200])


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    chat_id = str(update.effective_chat.id)
    text = update.message.text.strip()

    user = roster.get(chat_id)
    if not user:
        log.info('denied chat_id=%s', chat_id)
        await update.message.reply_text('⛔ Not on the allowed list.')
        return

    if chat_id in _in_flight:
        await update.message.reply_text('⏳ Still working on your previous question.')
        return

    is_command = text.startswith('/')
    is_video = bool(video_intel and (text.lower().startswith('/video')
                                     or video_intel.extract_video_ids(text)))
    _in_flight.add(chat_id)
    try:
        if not is_command:
            await update.message.chat.send_action(ChatAction.TYPING)
        # Answer the link straight away, then send the result when it is ready. A transcript
        # plus a model call per chunk takes far longer than a question does, and silence for
        # two minutes reads as the bot being broken.
        if is_video:
            ack = _video_ack(chat_id)
            await update.message.reply_text(ack)
            await _speak(update.message, ack, user)
        started = time.perf_counter()
        # botffet.answer is blocking (subprocess + CSV scan); keep it off the event loop so
        # one person's 12-18s question cannot stall everyone else's.
        reply = await asyncio.wait_for(
            asyncio.to_thread(botffet.answer, text, user=chat_id),
            timeout=VIDEO_TIMEOUT if is_video else ANSWER_TIMEOUT)
        elapsed = time.perf_counter() - started
        log.info('chat_id=%s kind=%s calls=%d %.1fs', chat_id, reply.get('kind'),
                 reply.get('provider_calls', 0), elapsed)
        body = reply.get('text') or '(empty answer)'
    except asyncio.TimeoutError:
        limit = VIDEO_TIMEOUT if is_video else ANSWER_TIMEOUT
        log.warning('chat_id=%s timed out after %ss', chat_id, limit)
        body = (f'⚠️ 影片處理超過 {limit // 60} 分鐘仍未完成，已中止。逐字稿可能已經抓下來了，'
                '稍後再傳一次同一個連結試試。' if is_video
                else f'⚠️ Timed out after {limit}s. Commands still work — try /screen.')
    except Exception:
        log.exception('chat_id=%s failed', chat_id)
        body = ('⚠️ 處理這部影片時發生問題，已記錄下來。' if is_video
                else '⚠️ Something broke answering that. Commands still work — try /screen.')
    finally:
        _in_flight.discard(chat_id)

    for chunk in _chunks(body):
        await update.message.reply_text(chunk)
    await _speak(update.message, body, user)


def preflight():
    """Assert we are the bot we think we are before claiming the poll lock.

    Mirrors daily_group_digest.py:41-47. Guards against a rotated or mixed-up token quietly
    making this process the poller for some other bot.
    """
    import ssl
    import urllib.request

    import certifi

    # certifi's bundle, not ssl._create_unverified_context() as apply_batch.py:12 does — this
    # request carries the bot token, so the certificate genuinely matters. The machine's system
    # CA store rejects api.telegram.org ("self signed certificate in certificate chain");
    # certifi's does not.
    ctx = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(
            f'https://api.telegram.org/bot{TOKEN}/getMe', timeout=15, context=ctx) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    if not data.get('ok'):
        raise SystemExit(f'getMe failed: {data}')
    bot = data['result']
    if bot.get('id') != EXPECTED_BOT_ID:
        raise SystemExit(
            f"refusing to poll: token is bot {bot.get('id')}, expected {EXPECTED_BOT_ID}")
    return bot


def main():
    if not TOKEN:
        raise SystemExit('TELEGRAM_BOT_TOKEN not set in scripts/.env')
    bot = preflight()
    log.info('preflight ok: @%s (%s)', bot.get('username'), bot.get('id'))
    roster.get('preload')
    log.info('爸菲特 / Wanna Botffet starting — one poller only')

    app = ApplicationBuilder().token(TOKEN).concurrent_updates(4).build()
    # filters.TEXT alone, NOT `& ~filters.COMMAND`: the old handler silently discarded every
    # /screen and /brief before it reached the router.
    app.add_handler(MessageHandler(filters.TEXT, handle_message))
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
