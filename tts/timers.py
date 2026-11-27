# -*- coding: utf-8 -*-
"""
timers.py — タイマー読み上げ

- /timer で「◯分後にこの文章を読んで」を予約する
- 予約は tts_config.json に保存されるので、BOTが再起動しても残る
- 時間になったら: VCにいれば読み上げ、いなければ入室を試み、
  それでも無理なら読み上げチャンネルへテキストで知らせる
- 15秒ごとの監視ループは on_ready で一度だけ起動する
"""

import asyncio
import time

from core import bot, store, DEFAULT_VOICE
from speech import get_session, enqueue

_loop_started = False


def list_timers(guild_id: int) -> list[dict]:
    cfg = store.cfg(guild_id)
    return sorted(cfg.get("timers", []), key=lambda t: t.get("fire_at", 0))


def add_timer(guild_id: int, fire_at: float, text: str,
              user_id: int, user_name: str) -> dict:
    cfg = store.cfg(guild_id)
    timers = list(cfg.get("timers", []))
    item = {"fire_at": fire_at, "text": text,
            "user_id": user_id, "user_name": user_name}
    timers.append(item)
    cfg["timers"] = timers
    store.save()
    return item


def cancel_timer(guild_id: int, index: int) -> dict | None:
    """表示順（時刻順）の index 番を削除して返す。"""
    cfg = store.cfg(guild_id)
    timers = list_timers(guild_id)
    if not (0 <= index < len(timers)):
        return None
    item = timers.pop(index)
    cfg["timers"] = timers
    store.save()
    return item


async def _fire(guild, item: dict):
    cfg = store.cfg(guild.id)
    session = get_session(guild.id)
    if not session.connected():
        try:
            from resident import evaluate
            await evaluate(guild)
        except Exception:
            pass
    text = item.get("text") or "時間になりました"
    if session.connected():
        await enqueue(session, f"⏰ タイマーです。{text}",
                      cfg.get("default_voice", DEFAULT_VOICE),
                      cfg.get("rate", "+0%"))
        return
    # VCに入れなかったらテキストで知らせる
    channel = None
    if cfg.get("read_channel_id"):
        channel = guild.get_channel(cfg["read_channel_id"])
    if channel is None and cfg.get("panel"):
        channel = guild.get_channel(cfg["panel"].get("channel", 0))
    if channel is not None:
        try:
            await channel.send(f"⏰ タイマー: {text}（VC未接続のため読み上げできませんでした）")
        except Exception:
            pass


async def _loop():
    while True:
        try:
            now = time.time()
            for guild in list(bot.guilds):
                cfg = store.cfg(guild.id)
                timers = list(cfg.get("timers", []))
                due = [t for t in timers if t.get("fire_at", 0) <= now]
                if due:
                    cfg["timers"] = [t for t in timers if t.get("fire_at", 0) > now]
                    store.save()
                    for item in due:
                        asyncio.create_task(_fire(guild, item))
        except Exception as e:
            print(f"⚠️ タイマー監視でエラー: {e}")
        await asyncio.sleep(15)


@bot.listen("on_ready")
async def _start_loop():
    global _loop_started
    if not _loop_started:
        _loop_started = True
        asyncio.create_task(_loop())
