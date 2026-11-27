# -*- coding: utf-8 -*-
"""
speech.py — 読み上げエンジン v2

- 発言テキストの整形（URL・メンション・絵文字・スポイラー等の処理、単語辞書、文字数カット）
- Edge TTS（無料・キー不要）で音声を合成し、キュー順にVCで再生する
- 対象テキストチャンネルの発言と、BOTがいるVC内チャットを拾う on_message リスナー
- v2の変更: 再生完了待ちを is_playing ポーリングから
  play(after=...) + asyncio.Event に変更（CPU・誤検出を減らす）。
"""

import asyncio
import itertools
import re
from pathlib import Path
from typing import Optional

import discord

from core import bot, store, VOICES, TMP_DIR, QUEUE_MAX, voice_of

# ---------------------------------------------------------------- テキスト整形
_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")
_CODE_BLOCK_RE = re.compile(r"```.*?```", re.S)
_SPOILER_RE = re.compile(r"\|\|(.*?)\|\|", re.S)
_MENTION_USER_RE = re.compile(r"<@!?(\d+)>")
_MENTION_CHANNEL_RE = re.compile(r"<#(\d+)>")
_MENTION_ROLE_RE = re.compile(r"<@&(\d+)>")


def clean_text(message: discord.Message, cfg: dict) -> str:
    """発言を読み上げ用に整形する。空文字なら読み上げ対象外。"""
    text = message.content or ""
    text = _CODE_BLOCK_RE.sub(" コード省略 ", text)
    text = _SPOILER_RE.sub(" スポイラー省略 ", text)
    text = _URL_RE.sub(" URL省略 ", text)
    text = _CUSTOM_EMOJI_RE.sub("", text)

    def _user(m):
        member = message.guild.get_member(int(m.group(1))) if message.guild else None
        return member.display_name if member else "誰か"
    text = _MENTION_USER_RE.sub(_user, text)

    def _channel(m):
        ch = message.guild.get_channel(int(m.group(1))) if message.guild else None
        return ch.name if ch else "チャンネル"
    text = _MENTION_CHANNEL_RE.sub(_channel, text)

    def _role(m):
        role = message.guild.get_role(int(m.group(1))) if message.guild else None
        return role.name if role else "ロール"
    text = _MENTION_ROLE_RE.sub(_role, text)

    text = text.replace("`", "")
    # 単語辞書（長い単語から先に置換）
    dictionary = cfg.get("dictionary", {})
    for word in sorted(dictionary, key=len, reverse=True):
        if word:
            text = text.replace(word, dictionary[word])
    text = re.sub(r"\s+", " ", text).strip()
    limit = int(cfg.get("max_chars", 100))
    if len(text) > limit:
        text = text[:limit]
    return text


# ---------------------------------------------------------------- セッションと再生キュー
class Session:
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self.voice: Optional[discord.VoiceClient] = None
        self.queue: asyncio.Queue = asyncio.Queue()
        self.worker: Optional[asyncio.Task] = None
        self.alone_task: Optional[asyncio.Task] = None
        self.rejoin_task: Optional[asyncio.Task] = None
        self.manual_left: bool = False   # 手動退出したら自動復帰しない
        self.now_text: Optional[str] = None

    def connected(self) -> bool:
        return bool(self.voice and self.voice.is_connected())


SESSIONS: dict[int, Session] = {}
_counter = itertools.count(1)


def get_session(guild_id: int) -> Session:
    if guild_id not in SESSIONS:
        SESSIONS[guild_id] = Session(guild_id)
    return SESSIONS[guild_id]


async def synthesize(text: str, voice_key: str, rate: str) -> Path:
    """Edge TTS で mp3 を作って返す（呼び出し側で削除する）。"""
    import edge_tts
    TMP_DIR.mkdir(exist_ok=True)
    path = TMP_DIR / f"tts_{next(_counter)}.mp3"
    voice = VOICES.get(voice_key, VOICES["nanami"])[0]
    communicate = edge_tts.Communicate(text, voice, rate=rate)
    await communicate.save(str(path))
    return path


async def enqueue(session: Session, text: str, voice_key: str, rate: str):
    if session.queue.qsize() >= QUEUE_MAX:
        return
    await session.queue.put((text, voice_key, rate))
    if session.worker is None or session.worker.done():
        session.worker = asyncio.create_task(_worker(session))


def _dave_state(session: Session):
    """DAVE暗号化の状態を (protocol_version, ready, session_exists) で返す。

    discord.py 2.7.1 の can_encrypt は
    protocol_version != 0 and session あり and session.ready。
    False のまま play() すると暗号化なしの opus が送られ、
    DAVE 通話のクライアントに黙って捨てられる（緑枠は出る・無音）。
    """
    conn = getattr(session.voice, "_connection", None) if session.voice else None
    if conn is None:
        return (0, False, False)
    dave = getattr(conn, "dave_session", None)
    return (
        getattr(conn, "dave_protocol_version", 0) or 0,
        bool(dave and getattr(dave, "ready", False)),
        dave is not None,
    )


async def _wait_encrypt(session: Session, timeout: float = 10.0) -> bool:
    """暗号化が完了するまで待つ。DAVEを使わない接続なら即 True。

    True = 暗号化して送れる / DAVEなしで送ってよい。
    False = DAVEセッションはあるのに ready にならない（鍵交換が来ていない）。
    """
    import time
    deadline = time.monotonic() + timeout
    while True:
        version, ready, exists = _dave_state(session)
        if not exists:
            return True  # DAVEセッションなし = 暗号化なしの通話なのでそのまま送ってよい
        if version == 0:
            return True  # プロトコル0 = パススルー（暗号化なしが受け入れられる状態）
        if ready:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.5)


async def _worker(session: Session):
    """キューを順番に合成して再生する（割り込みなし）。"""
    while True:
        try:
            text, voice_key, rate = await asyncio.wait_for(session.queue.get(), timeout=60)
        except asyncio.TimeoutError:
            return
        if not session.connected():
            _drain(session)
            return
        session.now_text = text
        path = None
        try:
            path = await synthesize(text, voice_key, rate)
            if not session.connected():
                return
            # 暗号化が未完了のまま再生しても相手には届かない（無音）ので、
            # DAVEセッションがある場合は完了まで最大10秒待つ。
            # 待っても ready にならない場合は捨てて次へ（無音再生をしない）。
            if not await _wait_encrypt(session, timeout=10.0):
                version, ready, _exists = _dave_state(session)
                print(f"⚠️ 暗号化未完了のため読み上げを保留 ({session.guild_id}): "
                      f"protocol={version} ready={ready} text={text[:20]}")
                continue
            if not session.connected():
                return
            done = asyncio.Event()
            loop = asyncio.get_running_loop()

            def _after(_err=None):
                loop.call_soon_threadsafe(done.set)

            source = discord.FFmpegPCMAudio(str(path), options="-vn")
            session.voice.play(source, after=_after)
            try:
                await asyncio.wait_for(done.wait(), timeout=180)
            except asyncio.TimeoutError:
                try:
                    session.voice.stop()
                except Exception:
                    pass
        except Exception as e:
            print(f"⚠️ 読み上げに失敗 ({session.guild_id}): {e}")
        finally:
            session.now_text = None
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass


def _drain(session: Session):
    while not session.queue.empty():
        try:
            session.queue.get_nowait()
        except asyncio.QueueEmpty:
            break


def skip_current(session: Session):
    if session.voice and (session.voice.is_playing() or session.voice.is_paused()):
        session.voice.stop()


def clear_queue(session: Session):
    _drain(session)


# ---------------------------------------------------------------- 発言を拾う
@bot.listen("on_message")
async def _on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return
    cfg = store.cfg(message.guild.id)
    if not cfg.get("enabled", True):
        return
    session = get_session(message.guild.id)
    is_read_channel = cfg.get("read_channel_id") == message.channel.id
    is_vc_chat = (
        cfg.get("read_vc_chat", True)
        and getattr(message.channel, "type", None) == discord.ChannelType.voice
        and session.connected()
        and session.voice.channel is not None
        and session.voice.channel.id == message.channel.id
    )
    if not (is_read_channel or is_vc_chat):
        return
    raw = (message.content or "").strip()
    if not raw or raw.startswith(("/", "!", "！")):
        return
    if not session.connected():
        return
    setting = cfg["users"].get(str(message.author.id), {})
    if setting.get("muted"):
        return
    if cfg.get("only_vc_members"):
        voice = getattr(message.author, "voice", None)
        if not voice or not voice.channel or session.voice.channel != voice.channel:
            return
    text = clean_text(message, cfg)
    if not text:
        return
    if cfg.get("read_name", True):
        text = f"{message.author.display_name}、{text}"
    await enqueue(session, text, voice_of(cfg, message.author.id), cfg.get("rate", "+0%"))
