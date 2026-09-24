#!/usr/bin/env python3
"""爸菲特's voice — every message it sends also arrives as a Telegram voice bubble.

Why: Peter and his dad read 爸菲特 on the phone, and a spoken reply plays anywhere, without
squinting at a screen. Peter's brief for the voice: an older Taiwanese man, natural, warm —
"像台灣以前的孫叔叔" — and explicitly not a robot.

How: text is cleaned into something listenable (`speakable`), spoken by one of two engines,
and transcoded with ffmpeg to Opus-in-Ogg, the only format Telegram renders as a playable voice
bubble rather than a file attachment.

* **Edge TTS** (`edge_tts`, free, no key, a network call) — the natural voices. The default is
  zh-TW-YunJheNeural (雲哲), the one Taiwanese-Mandarin male Edge publishes, slowed and
  pitched down a touch. Peter auditioned three candidates on 2026-08-29 and chose this
  setting (-5% / -8Hz) over a heavier -12% / -20Hz and over macOS's "Grandpa" voice.
* **macOS `say`** (offline) — for Apple's bundled voices such as "Grandpa (Chinese (Taiwan))",
  an elderly-man voice that is an alternative if the Edge one reads too young.

Settings live in `botffet_voice.json` beside this file (optional; DEFAULTS apply without it) so
the voice can be changed without touching code or the research loop's config. Per person,
`"voice": false` in telegram_users.json switches the bubble off for that chat.

Voice must never break a reply: the text always goes first, and any failure here is logged and
swallowed by the caller.

Edge is a free public service and it hiccups: on 2026-09-16 19:14 it held a video summary's
request for 36 s and then closed with no audio ("No audio was received"), while the same text
synthesised 13/13 times minutes later. So Edge gets a few bounded tries, and if it still gives
nothing the words are spoken offline by macOS `say` (Grandpa, the elderly Taiwanese voice) —
a different voice is far better than a text with no bubble. `report` tells the caller which
engine spoke and how many tries it took, so the log can say so.
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(SCRIPTS_DIR, 'botffet_voice.json')

DEFAULTS = {
    'enabled': True,
    'engine': 'edge',                  # 'edge' | 'say'
    'voice': 'zh-TW-YunJheNeural',     # edge voice id, or a macOS `say` voice name
    'rate': '-5%',                     # edge: percentage. say: applied to SAY_BASE_WPM
    'pitch': '-8Hz',                   # edge only
    'max_chars': 1500,                 # roughly five spoken minutes of Chinese
    'retries': 2,                      # extra Edge tries after the first (3 in all)
    'fallback_engine': 'say',          # spoken offline when Edge gives nothing; '' disables
    'fallback_voice': 'Grandpa (Chinese (Taiwan))',
}

# Seconds to wait between Edge tries; the last value repeats if there are more tries.
RETRY_PAUSES = (2, 5)
# Per-try bounds handed to edge-tts (its own default is 60 s to receive). A hung request
# should cost half a minute, not the open-ended wait it cost on 2026-09-16.
EDGE_CONNECT_TIMEOUT = 10
EDGE_RECEIVE_TIMEOUT = 30

# `say -r` is words per minute; the percentage rate is applied to its default so one setting
# means the same thing on both engines.
SAY_BASE_WPM = 175

# Edge synthesises ~5,000 characters per call; keep well under it.
HARD_MAX_CHARS = 4000


class VoiceError(RuntimeError):
    pass


class TransientVoiceError(VoiceError):
    """The service or the link failed this time; the same request may well work next time."""


_pause = time.sleep          # module attribute so tests can make the retry pauses instant


def settings(path=None):
    """DEFAULTS overlaid with the optional settings file. A broken file means defaults, not
    silence — the message already went out as text, and a bad JSON edit should not mute the
    bot without anyone noticing."""
    cfg = dict(DEFAULTS)
    try:
        with open(path or SETTINGS_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    cfg['max_chars'] = max(1, min(int(cfg.get('max_chars') or DEFAULTS['max_chars']),
                                  HARD_MAX_CHARS))
    return cfg


# --- availability --------------------------------------------------------------------------

def ffmpeg_path():
    """launchd and nohup shells get a trimmed PATH; check the Homebrew prefix explicitly."""
    return shutil.which('ffmpeg') or next(
        (p for p in ('/opt/homebrew/bin/ffmpeg', '/usr/local/bin/ffmpeg') if os.path.exists(p)),
        None)


def _say_available():
    return bool(shutil.which('say'))


def _fallback_configured(cfg):
    """True when a missing or failing Edge can be covered by the offline `say` engine."""
    return (cfg.get('engine') != 'say' and (cfg.get('fallback_engine') or '') == 'say'
            and _say_available())


def unavailable_reason(cfg=None):
    """Why voice cannot work right now, or None if it can."""
    cfg = cfg or settings()
    if not cfg.get('enabled'):
        return 'voice is switched off in botffet_voice.json'
    if not ffmpeg_path():
        return 'ffmpeg is not installed; Telegram voice bubbles need Opus audio'
    if cfg.get('engine') == 'say':
        return None if _say_available() else "the macOS 'say' command is not available"
    try:
        import edge_tts  # noqa: F401
    except Exception:  # noqa: BLE001
        if _fallback_configured(cfg):
            return None          # Edge cannot run, but the fallback can still speak
        return 'the edge-tts library is not installed for this Python'
    return None


# --- text -> something worth listening to --------------------------------------------------

_FENCED = re.compile(r'```.*?```', re.DOTALL)
_TABLE_ROW = re.compile(r'^\s*\|.*\|\s*$', re.MULTILINE)
_MD_LINK = re.compile(r'\[([^\]]+)\]\((?:[^)]+)\)')
_BARE_URL = re.compile(r'https?://\S+')
_COMMAND = re.compile(r'(?<![\w/])/([A-Za-z_]+)')        # "/memory" -> "memory"
_BULLET = re.compile(r'^\s*(?:[-*•]|\d+[.)、])\s+', re.MULTILINE)
_MD_CHARS = re.compile(r'[*_#>`|~]+')
# Pictographs, symbols, dingbats, flags, variation selectors and joiners. A voice reading
# "warning sign" before every caution line is exactly the robot Peter did not want.
_EMOJI = re.compile(
    '[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF'
    '\U0000FE0F\U0000200D\U00002B00-\U00002BFF\U00002300-\U000023FF]+')
_SENTENCE_END = re.compile(r'[。！？!?.]\s*|\n')


def speakable(text, max_chars=None):
    """Strip what cannot be listened to, then cut at a sentence end under `max_chars`.

    Code blocks and table rows go entirely (their content is only readable). Links keep their
    label, bare URLs vanish, emoji vanish, markdown markup vanishes, list markers become
    pauses. The full text is in the message that precedes the bubble, so cutting is safe.
    """
    max_chars = max_chars or settings()['max_chars']
    t = text or ''
    t = _FENCED.sub(' ', t)
    t = _TABLE_ROW.sub(' ', t)
    t = _MD_LINK.sub(r'\1', t)
    t = _BARE_URL.sub(' ', t)
    t = _COMMAND.sub(r'\1', t)
    t = _BULLET.sub('', t)
    t = _MD_CHARS.sub('', t)
    t = _EMOJI.sub('', t)
    t = re.sub(r'[ \t]+', ' ', t)
    t = re.sub(r'\n\s*\n+', '\n', t).strip()
    if len(t) <= max_chars:
        return t
    cut = 0
    for m in _SENTENCE_END.finditer(t):
        if m.end() > max_chars:
            break
        cut = m.end()
    return (t[:cut] if cut else t[:max_chars]).strip()


# --- synthesis -----------------------------------------------------------------------------

def _to_ogg(src, dst):
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise VoiceError('ffmpeg not found')
    res = subprocess.run([ffmpeg, '-y', '-loglevel', 'error', '-i', src,
                          # 32k mono Opus is transparent for speech and a third smaller than
                          # the 48k it was: the upload to Telegram, not the synthesis, is
                          # what times out on this Mac's link (2026-09-03).
                          '-c:a', 'libopus', '-b:a', '32k', '-ac', '1', '-ar', '48000', dst],
                         capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        raise VoiceError(f'ffmpeg failed: {(res.stderr or "")[-300:]}')


def _edge(text, cfg, out_mp3):
    try:
        import edge_tts
    except Exception as exc:  # noqa: BLE001
        raise VoiceError(f'edge-tts unavailable: {exc}')

    async def run():
        comm = edge_tts.Communicate(text, cfg['voice'], rate=cfg.get('rate') or '+0%',
                                    pitch=cfg.get('pitch') or '+0Hz',
                                    connect_timeout=EDGE_CONNECT_TIMEOUT,
                                    receive_timeout=EDGE_RECEIVE_TIMEOUT)
        await comm.save(out_mp3)
    # Own loop on purpose, and never the caller's: the bot calls this from a worker thread,
    # but a script that is itself async (botffet_push) would hit "asyncio.run() cannot be
    # called from a running event loop" — so if a loop is running here, do the work on a
    # short-lived thread of its own.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(run())
    else:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(asyncio.run, run()).result()
    if not os.path.exists(out_mp3) or os.path.getsize(out_mp3) == 0:
        raise TransientVoiceError('edge-tts produced no audio')


def _say(text, cfg, out_aiff):
    pct = int(re.sub(r'[^-\d]', '', str(cfg.get('rate') or '0')) or 0)
    wpm = max(90, min(300, round(SAY_BASE_WPM * (100 + pct) / 100)))
    res = subprocess.run(['say', '-v', cfg['voice'], '-r', str(wpm), '-o', out_aiff, text],
                         capture_output=True, text=True, timeout=300)
    if res.returncode != 0:
        raise VoiceError(f'say failed: {(res.stderr or "")[-300:]}')


def _is_transient(exc):
    """Worth another try: the service or the link failed, not our parameters.

    A ValueError from edge-tts means a bad voice name or rate — retrying cannot help.
    """
    if isinstance(exc, TransientVoiceError):
        return True
    if isinstance(exc, VoiceError):
        return False
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError, OSError)):
        return True
    try:
        from edge_tts import exceptions as edge_exc
        if isinstance(exc, (edge_exc.NoAudioReceived, edge_exc.WebSocketError,
                            edge_exc.UnexpectedResponse, edge_exc.UnknownResponse)):
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        import aiohttp
        if isinstance(exc, aiohttp.ClientError):
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _edge_with_retries(text, cfg, tmp, report):
    """Edge, up to 1 + `retries` times, pausing between tries. Returns the mp3 path.

    Every failed try is recorded in report['errors']; the last error is raised (a
    non-transient one immediately) for the caller to fall back on.
    """
    tries = 1 + max(0, int(cfg.get('retries') or 0))
    for attempt in range(1, tries + 1):
        src = os.path.join(tmp, f'voice-{attempt}.mp3')
        report['attempts'] = attempt
        try:
            _edge(text, cfg, src)
            return src
        except Exception as exc:  # noqa: BLE001
            report['errors'].append(
                f'edge try {attempt}: {type(exc).__name__}: {str(exc)[:160]}')
            if not _is_transient(exc) or attempt == tries:
                raise
            _pause(RETRY_PAUSES[min(attempt - 1, len(RETRY_PAUSES) - 1)])
    raise VoiceError('edge produced nothing')       # unreachable; keeps the type checker calm


def _fallback(text, cfg, tmp, report, cause):
    """Speak offline with `say` after Edge gave up, or raise the reason when that is off."""
    if not _fallback_configured(cfg):
        raise VoiceError(f'edge failed after {report["attempts"]} attempt(s): '
                         f'{str(cause)[:200]}') from cause
    voice = cfg.get('fallback_voice') or DEFAULTS['fallback_voice']
    src = os.path.join(tmp, 'voice.aiff')
    _say(text, dict(cfg, voice=voice), src)
    report.update({'engine': 'say', 'voice': voice, 'fallback': True})
    return src


def synthesize(text, cfg=None, report=None):
    """Spoken `text` as Opus-in-Ogg bytes, ready for Telegram's sendVoice.

    `text` should already be `speakable()`; this does not clean it again, so a caller can
    also hand over a sentence written specifically to be heard.

    Pass a dict as `report` to learn what happened: which engine and voice spoke, how many
    tries it took, the seconds spent, each failed try's error, and whether the offline
    fallback was used. The dict is filled in even when this raises.
    """
    cfg = cfg or settings()
    report = report if isinstance(report, dict) else {}
    report.update({'engine': cfg.get('engine'), 'voice': cfg.get('voice'), 'attempts': 0,
                   'seconds': 0.0, 'errors': [], 'fallback': False})
    started = time.monotonic()
    try:
        reason = unavailable_reason(cfg)
        if reason:
            raise VoiceError(reason)
        text = (text or '').strip()
        if not text:
            raise VoiceError('nothing to say')
        with tempfile.TemporaryDirectory(prefix='botffet-voice-') as tmp:
            ogg = os.path.join(tmp, 'voice.ogg')
            if cfg.get('engine') == 'say':
                src = os.path.join(tmp, 'voice.aiff')
                report['attempts'] = 1
                _say(text, cfg, src)
            else:
                try:
                    src = _edge_with_retries(text, cfg, tmp, report)
                except Exception as exc:  # noqa: BLE001
                    src = _fallback(text, cfg, tmp, report, exc)
            _to_ogg(src, ogg)
            with open(ogg, 'rb') as f:
                return f.read()
    finally:
        report['seconds'] = round(time.monotonic() - started, 1)


def voice_for(text, cfg=None, report=None):
    """speakable + synthesize in one step; returns None (never raises) when there is nothing
    listenable left, so a caller can `if data: send`."""
    cfg = cfg or settings()
    spoken = speakable(text, cfg['max_chars'])
    if not spoken:
        return None
    return synthesize(spoken, cfg, report)


if __name__ == '__main__':
    # Quick ear test: python3 botffet_voice.py "你好" out.ogg [edge|say]
    # The third argument picks the engine; `say` uses the fallback voice, so the backup can be
    # auditioned without switching Edge off.
    import sys
    cfg = settings()
    if len(sys.argv) > 3 and sys.argv[3] == 'say':
        cfg.update(engine='say', voice=cfg['fallback_voice'])
    info = {}
    data = voice_for(sys.argv[1] if len(sys.argv) > 1 else '老傢伙，我是爸菲特。', cfg, info)
    out = sys.argv[2] if len(sys.argv) > 2 else 'voice.ogg'
    with open(out, 'wb') as f:
        f.write(data or b'')
    print(f'{len(data or b"")} bytes -> {out}  {info}')
