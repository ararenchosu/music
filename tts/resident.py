# -*- coding: utf-8 -*-
"""
resident.py — VC常駐ロジック v2

- 誰かがVCにいる間だけBOTが常駐し、全員いなくなったら退出する
- 対象VCは「自動（人がいるVC）」か「固定VC」を設定で切り替え
- 入退室あいさつ（読み上げキュー経由）
- v2の変更: 接続失敗のログを「タイムアウト」と「その他」で分けた。
  接続の合図は通るのに音声が届かないホスト問題と、コードの問題を
  ログだけで切り分けられるようにするため。
  self_deaf は False のまま（スピーカーミュート表示にしない）。
"""

import asyncio
from typing import Optional

import discord

from core import bot, store, ALONE_LEAVE_SEC, DEFAULT_VOICE
from speech import get_session, enqueue


def _humans(channel: discord.VoiceChannel) -> list:
    return [m for m in channel.members if not m.bot]


def target_channel(guild: discord.Guild, session) -> Optional[discord.VoiceChannel]:
    """今BOTがいるべきVCを返す（誰もいなければ None）。"""
    cfg = store.cfg(guild.id)
    fixed_id = cfg.get("target_vc_id")
    if fixed_id:
        ch = guild.get_channel(fixed_id)
        if ch is not None and _humans(ch):
            return ch
        return None
    # 自動: 今いるVCにまだ人がいれば維持、いなければ一番人数が多いVCへ
    if session.connected() and session.voice.channel is not None:
        if _humans(session.voice.channel):
            return session.voice.channel
    best = None
    for ch in guild.voice_channels:
        n = len(_humans(ch))
        if n and (best is None or n > len(_humans(best))):
            best = ch
    return best


async def _connect(session, channel: discord.VoiceChannel):
    """VC接続。タイムアウトは専用ログにする。"""
    try:
        session.voice = await channel.connect(self_deaf=False)
    except asyncio.TimeoutError:
        print(f"⚠️ VC接続タイムアウト ({channel.guild.id}): 接続の合図は通ったが音声接続が30秒で切れた")
        session.voice = None
        raise
    except Exception as e:
        print(f"⚠️ VC接続に失敗 ({channel.guild.id}): {e}")
        session.voice = None
        raise


async def evaluate(guild: discord.Guild):
    """VCの状況に合わせて 接続 / 移動 / 退出予約 を行う。"""
    session = get_session(guild.id)
    target = target_channel(guild, session)
    if target is None:
        if session.connected():
            _schedule_leave(guild, session)
        return
    _cancel_leave(session)
    try:
        if not session.connected():
            await _connect(session, target)
        elif session.voice.channel.id != target.id:
            await session.voice.move_to(target)
    except Exception:
        pass  # 詳細は _connect がログ済み


def _cancel_leave(session):
    if session.alone_task and not session.alone_task.done():
        session.alone_task.cancel()
    session.alone_task = None


def _schedule_leave(guild: discord.Guild, session):
    if session.alone_task and not session.alone_task.done():
        return

    async def _job():
        try:
            await asyncio.sleep(ALONE_LEAVE_SEC)
            if target_channel(guild, session) is None and session.connected():
                await leave(guild)
        except asyncio.CancelledError:
            pass

    session.alone_task = asyncio.create_task(_job())


async def leave(guild: discord.Guild):
    session = get_session(guild.id)
    _cancel_leave(session)
    if session.voice and session.voice.is_connected():
        await session.voice.disconnect(force=True)
    session.voice = None


async def manual_join(guild: discord.Guild, member: discord.Member):
    """手動入室: 押した人のVCを優先し、無ければ通常の対象VCへ。

    BOT先行テスト用: 固定VCが指定されていれば、誰もいなくても
    そのVCに入れる（従来は誰もいないと「入室先がありません」で失敗した）。
    空のVCに入ったBOTは、誰かが入るまで evaluate が走らないので
    15秒の自動退出は始まらない。
    """
    session = get_session(guild.id)
    _cancel_leave(session)
    channel = None
    if member.voice and member.voice.channel:
        channel = member.voice.channel
    else:
        channel = target_channel(guild, session)
        if channel is None:
            fixed_id = store.cfg(guild.id).get("target_vc_id")
            if fixed_id:
                channel = guild.get_channel(fixed_id)
    if channel is None:
        raise discord.DiscordException("入室先のVCがありません（誰もVCにいません）")
    if not session.connected():
        await _connect(session, channel)
    elif session.voice.channel.id != channel.id:
        await session.voice.move_to(channel)
    return channel


@bot.listen("on_ready")
async def _evaluate_all_on_ready():
    for guild in bot.guilds:
        await evaluate(guild)


@bot.event
async def on_voice_state_update(member: discord.Member, before, after):
    guild = member.guild
    session = get_session(guild.id)
    cfg = store.cfg(guild.id)

    # あいさつ（BOTが既にいるVCへの出入りだけ）
    if not member.bot and cfg.get("greetings", True) and session.connected():
        bot_channel = session.voice.channel
        joined = (after.channel is not None and after.channel.id == bot_channel.id
                  and (before.channel is None or before.channel.id != bot_channel.id))
        left = (before.channel is not None and before.channel.id == bot_channel.id
                and (after.channel is None or after.channel.id != bot_channel.id))
        if joined:
            await enqueue(session, f"{member.display_name}さんが入りました",
                          cfg.get("default_voice", DEFAULT_VOICE), cfg.get("rate", "+0%"))
        elif left:
            await enqueue(session, f"{member.display_name}さんが退出しました",
                          cfg.get("default_voice", DEFAULT_VOICE), cfg.get("rate", "+0%"))

    if not member.bot:
        await evaluate(guild)
