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
        cap = botffet._video_cap(chat_id)
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
    # `report` says which engine spoke and how many tries it took (botffet_voice.synthesize
    # retries Edge and falls back to the offline `say` voice). Every outcome is logged, the
    # successes too: on 2026-09-16 the log could not answer "did the bubble go out?".
    report = {}
    try:
        data = await asyncio.to_thread(botffet_voice.voice_for, text, None, report)
    except Exception as exc:  # noqa: BLE001
        log.warning('voice skipped for chat_id=%s after %s tr%s: %s', message.chat_id,
                    report.get('attempts') or '?', 'y' if report.get('attempts') == 1 else 'ies',
                    '; '.join(report.get('errors') or [str(exc)[:200]])[:400])
        await _notify_voice_lost(user, message.chat_id, f'語音合成失敗：{str(exc)[:200]}')
        return
    if not data:
        return
    if report.get('fallback'):
        log.warning('voice via fallback %s/%s for chat_id=%s after edge failed: %s',
                    report.get('engine'), report.get('voice'), message.chat_id,
                    '; '.join(report.get('errors') or [])[:400])
    # The upload is the fragile half: synthesis had already succeeded every time the log said
    # "voice skipped ... Timed out". One retry after a pause covers a momentary bad gateway
    # without turning a dead link into a minute-long stall.
    started = time.perf_counter()
    for attempt in (1, 2):
        try:
            await message.reply_voice(voice=io.BytesIO(data),
                                      read_timeout=60, write_timeout=300)
            log.info('voice sent chat_id=%s engine=%s attempts=%s synth=%.1fs upload=%.1fs '
                     'bytes=%d', message.chat_id, report.get('engine'),
                     report.get('attempts'), report.get('seconds') or 0.0,
                     time.perf_counter() - started, len(data))
            return
        except Exception as exc:  # noqa: BLE001
            log.warning('voice upload attempt %d failed for chat_id=%s (%d bytes): %s',
                        attempt, message.chat_id, len(data), str(exc)[:200])
            if attempt == 1:
                await asyncio.sleep(3)
    await _notify_voice_lost(user, message.chat_id, '語音檔上傳 Telegram 失敗（連線問題）')


# Text delivery retries. On 2026-09-03 22:49 a 70-minute video for Dad — transcribed from audio
# and summarised over twenty minutes — was lost at the very last step: the one reply_text()
# call hit a ConnectError and nothing retried it. The notes were stored; the person saw
# nothing. The link to Telegram is flaky (see the timeouts above), so the reply, the part
# that matters, gets the same patience the voice upload already has: four tries over ~45 s.
DELIVER_PAUSES = (3, 10, 30)


async def _deliver(message, body, kind=None):
    """Send `body` in Telegram-sized chunks, retrying each on a network error.

    Returns True when every chunk went out. Only network trouble is retried; any other
    failure is logged and gives up at once, so a bad request cannot loop, and the handler
    never dies with "No error handlers are registered" the way it did before.
    """
    from telegram.error import NetworkError
    for chunk in _chunks(body):
        for attempt, pause in enumerate((*DELIVER_PAUSES, None), 1):
            try:
                await message.reply_text(chunk)
                break
            except NetworkError as exc:
                if pause is None:
                    log.error('reply LOST for chat_id=%s kind=%s after %d attempts: %s',
                              message.chat_id, kind, attempt, str(exc)[:200])
                    return False
                log.warning('reply delivery attempt %d failed for chat_id=%s: %s',
                            attempt, message.chat_id, str(exc)[:200])
                await asyncio.sleep(pause)
            except Exception as exc:  # noqa: BLE001
                log.error('reply LOST for chat_id=%s kind=%s (not retryable): %s',
                          message.chat_id, kind, str(exc)[:200])
                return False
    return True


# A video that did not come back as notes is reported to Peter — not in this chat, where the
# person who sent the link already sees the failure, but through the Orchestrator bot
# (@Panbear_Orchestrator_bot), the same channel the research loop uses for its own alerts.
# Peter asked for this on 2026-09-04 after Dad's first video was lost without anyone knowing.
VIDEO_FAILURE_KINDS = ('video_error', 'video_no_transcript')


def _failure_reason(kind, body, is_video):
    """Why a video request counts as unsuccessful, or None when it does not."""
    if not is_video:
        return None
    if kind in VIDEO_FAILURE_KINDS:
        return (body or '').strip().splitlines()[0][:200]
    return None


def _video_failure_report(user, chat_id, text, reason):
    """The alert text, in Chinese, with everything needed to act on it."""
    link = ''
    if video_intel:
        ids = video_intel.extract_video_ids(text or '')
        if ids:
            link = video_intel.watch_url(ids[0])
    who = (user or {}).get('label') or chat_id
    return (f'⚠️ 爸菲特影片轉換失敗\n'
            f'使用者：{who}（{chat_id}）\n'
            f'連結：{link or "（訊息中沒有可辨識的連結）"}\n'
            f'原因：{reason}\n'
            f'時間：{time.strftime("%Y-%m-%d %H:%M")}')


def _operator_chat_id():
    """Peter's own chat, from the Orchestrator target the research loop already uses."""
    try:
        import apply_batch
        return apply_batch.ORCHESTRATOR_TELEGRAM_TARGET.split(':')[-1]
    except Exception:  # noqa: BLE001
        return ''


async def _notify_operator(chat_id, report, what):
    """Send `report` to Peter through the Orchestrator bot. Never raises; never blocks the
    loop. `what` names the event in the log line ("video failure", "voice lost")."""
    if str(chat_id) == _operator_chat_id():
        return  # Peter sees his own failures in this chat already
    try:
        import apply_batch
        sent = await asyncio.to_thread(apply_batch.send_telegram, report)
        log.warning('%s for chat_id=%s reported to operator: %s', what, chat_id,
                    'sent' if sent else 'SEND FAILED')
    except Exception as exc:  # noqa: BLE001
        log.error('%s for chat_id=%s could not be reported: %s', what, chat_id,
                  str(exc)[:200])


async def _notify_video_failure(user, chat_id, text, reason):
    """A video that did not come back as notes, reported to Peter (see VIDEO_FAILURE_KINDS)."""
    if str(chat_id) == _operator_chat_id():
        return
    await _notify_operator(chat_id, _video_failure_report(user, chat_id, text, reason),
                           'video failure')


async def _notify_voice_lost(user, chat_id, reason):
    """The text arrived but its voice bubble did not, after every try and the offline
    fallback. Rare by design, so worth a line to Peter when it is not his own chat."""
    if str(chat_id) == _operator_chat_id():
        return
    who = (user or {}).get('label') or chat_id
    report = (f'⚠️ 爸菲特語音未送達（文字已送達）\n'
              f'使用者：{who}（{chat_id}）\n'
              f'原因：{reason}\n'
              f'時間：{time.strftime("%Y-%m-%d %H:%M")}')
    await _notify_operator(chat_id, report, 'voice lost')


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    chat_id = str(update.effective_chat.id)
    text = update.message.text.strip()

    user = roster.get(chat_id)
    if not user:
        log.info('denied chat_id=%s', chat_id)
        await update.message.reply_text('⛔ 您不在允許使用的名單中。')
        return

    if chat_id in _in_flight:
        await update.message.reply_text('⏳ 上一個問題還在處理中，請稍候。')
        return

    is_command = text.startswith('/')
    is_video = bool(video_intel and (text.lower().startswith('/video')
                                     or video_intel.extract_video_ids(text)))
    _in_flight.add(chat_id)
    kind = None
    failure = None
    try:
        if not is_command:
            await update.message.chat.send_action(ChatAction.TYPING)
        # Answer the link straight away, then send the result when it is ready. A transcript
        # plus a model call per chunk takes far longer than a question does, and silence for
        # two minutes reads as the bot being broken.
        if is_video:
            ack = _video_ack(chat_id)
            if await _deliver(update.message, ack, 'video_ack'):
                await _speak(update.message, ack, user)
        started = time.perf_counter()
        # botffet.answer is blocking (subprocess + CSV scan); keep it off the event loop so
        # one person's 12-18s question cannot stall everyone else's.
        reply = await asyncio.wait_for(
            asyncio.to_thread(botffet.answer, text, user=chat_id),
            timeout=VIDEO_TIMEOUT if is_video else ANSWER_TIMEOUT)
        elapsed = time.perf_counter() - started
        kind = reply.get('kind')
        log.info('chat_id=%s kind=%s calls=%d %.1fs', chat_id, kind,
                 reply.get('provider_calls', 0), elapsed)
        body = reply.get('text') or '(empty answer)'
        failure = _failure_reason(kind, body, is_video)
    except asyncio.TimeoutError:
        limit = VIDEO_TIMEOUT if is_video else ANSWER_TIMEOUT
        log.warning('chat_id=%s timed out after %ss', chat_id, limit)
        body = (f'⚠️ 影片處理超過 {limit // 60} 分鐘仍未完成，已中止。逐字稿可能已經抓下來了，'
                '稍後再傳一次同一個連結試試。' if is_video
                else f'⚠️ 處理超過 {limit} 秒仍未完成，已中止。指令仍可使用，例如 /screen。')
        if is_video:
            failure = f'處理超過 {limit // 60} 分鐘仍未完成，已中止'
    except Exception:
        log.exception('chat_id=%s failed', chat_id)
        body = ('⚠️ 處理這部影片時發生問題，已記錄下來。' if is_video
                else '⚠️ 回答這個問題時發生錯誤，已記錄下來。指令仍可使用，例如 /screen。')
        if is_video:
            failure = '處理時發生程式錯誤（詳見 botffet_bot.log）'
    finally:
        _in_flight.discard(chat_id)

    # Text first, voice only once the text is known to have arrived.
    if await _deliver(update.message, body, kind):
        await _speak(update.message, body, user)
    elif is_video and not failure:
        failure = '結果已整理好，但傳回 Telegram 失敗（連線問題）；再貼一次連結即可取得'
    if failure:
        await _notify_video_failure(user, chat_id, text, failure)


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

    # The library's defaults are 5 s to read a reply and 20 s to upload media. A voice bubble
    # for a 700-character summary is ~750 KB, and this Mac's link to Telegram is flaky (dozens
    # of Bad Gateway / timeout entries in the log), so every video summary's voice was being
    # dropped with "Timed out" on 2026-09-03 while the text went through. Measured that day:
    # with NordVPN on, uploads from this Mac crawl at ~5 KB/s (download is fine), so a
    # 500 KB bubble needs ~100 s; straight out the Wi-Fi it is ~190 KB/s. 300 s covers the
    # VPN case. Text replies are tiny and never hit these limits.
    app = (ApplicationBuilder().token(TOKEN).concurrent_updates(4)
           .connect_timeout(15).read_timeout(30).write_timeout(30)
           .media_write_timeout(300).pool_timeout(5).build())
    # filters.TEXT alone, NOT `& ~filters.COMMAND`: the old handler silently discarded every
    # /screen and /brief before it reached the router.
    app.add_handler(MessageHandler(filters.TEXT, handle_message))
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
