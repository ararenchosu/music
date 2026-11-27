# -*- coding: utf-8 -*-
"""
player.py — 再生エンジン

- サーバーごとの再生状態 GuildPlayer（キュー / ループ / 音量 / フィルター）
- 再生・次曲送り・自動退出（無再生時 / BOTだけ残った時。常駐モードで無効化）
- 音量は PCMVolumeTransformer、フィルターは FFmpeg の -af で適用
  （フィルター変更は再生中の曲には遡らず、次曲から適用される）
"""

import asyncio
import random
import time
from typing import Optional

import discord
from discord.ext import commands

import store
from core import Track
from sources import (build_audio_source, build_audio_source_from_stream,
                     resolve_stream_url, resolve_query, is_playlist_query,
                     get_spotify_collection, search_with_fallback,
                     fetch_related_tracks)


async def _redraw(bot, player: "GuildPlayer"):
    """パネル再描画（panel.py との循環import回避のため遅延import）。"""
    from panel import redraw_panel
    await redraw_panel(bot, player)


async def _repost(bot, player: "GuildPlayer"):
    """パネル再設置（panel.py との循環import回避のため遅延import）。"""
    from panel import repost_panel
    await repost_panel(bot, player)


async def _update_sub(bot, player: "GuildPlayer", force: bool = False):
    from panel import update_sub_panel
    await update_sub_panel(bot, player, force=force)


async def _update_lyrics(bot, player: "GuildPlayer"):
    from panel import update_lyrics_panel
    await update_lyrics_panel(bot, player)


class GuildPlayer:
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self.queue: list[Track] = []
        self.current: Optional[Track] = None
        self.voice: Optional[discord.VoiceClient] = None
        self.loop_mode = "off"            # off / track / queue
        self.filter_key = "none"
        cfg = store.guild_config(guild_id)
        self.volume = int(cfg.get("default_volume", 100))  # 0-200 (%)
        self.session = 0
        self.lock = asyncio.Lock()
        self.idle_task: Optional[asyncio.Task] = None
        self.alone_task: Optional[asyncio.Task] = None
        self.text_channel_id: Optional[int] = None  # 再生開始通知の送り先
        # 再生進捗（サブパネル用）: monotonic ベースで一時停止時間を差し引く
        self.started_at: float = 0.0
        self.paused_since: Optional[float] = None
        self.paused_total: float = 0.0
        # 先読み（次の曲のストリームURLキャッシュ）とオートプレイの重複防止
        self.prefetch: dict[str, tuple[str, float]] = {}
        self.prefetch_pending: set[str] = set()
        self.played_urls: set[str] = set()
        self.sub_sig: str = ""  # サブパネルの最終表示署名（無駄な編集を避ける）
        # VC落ち対策: 最終接続先 / 再接続タスク / 意図的な切断フラグ
        self.last_voice_channel_id: Optional[int] = None
        self.rejoin_task: Optional[asyncio.Task] = None
        self.stopping: bool = False

    # ---- キュー操作
    def enqueue(self, track: Track) -> int:
        self.queue.append(track)
        # 再生中の裏で次の曲を先読みしておく（失敗しても無視）
        try:
            schedule_prefetch(self)
        except Exception:
            pass
        return len(self.queue)

    def shuffle_queue(self) -> None:
        random.shuffle(self.queue)

    def remove_at(self, index: int) -> Optional[Track]:
        if 0 <= index < len(self.queue):
            return self.queue.pop(index)
        return None

    def clear_queue(self) -> int:
        n = len(self.queue)
        self.queue.clear()
        return n

    # ---- タイマー
    def cancel_timers(self):
        for attr in ("idle_task", "alone_task"):
            task = getattr(self, attr)
            if task and not task.done():
                task.cancel()
            setattr(self, attr, None)

    def cancel_idle(self):
        if self.idle_task and not self.idle_task.done():
            self.idle_task.cancel()
        self.idle_task = None


# ---------------------------------------------------------------- 再生進捗・一時停止
def elapsed_seconds(player: GuildPlayer) -> float:
    """現在曲の再生経過秒数（一時停止中の時間は数えない）。"""
    if player.current is None or not player.started_at:
        return 0.0
    now = time.monotonic()
    elapsed = now - player.started_at - player.paused_total
    if player.paused_since is not None:
        elapsed -= now - player.paused_since
    return max(0.0, elapsed)


def progress_bar(player: GuildPlayer, blocks: int = 15) -> str:
    """進捗バー文字列。長さ不明（ライブ等）は空バー。"""
    cur = player.current
    if cur is None or not cur.duration:
        return "▱" * blocks
    ratio = min(1.0, elapsed_seconds(player) / cur.duration)
    filled = int(ratio * blocks)
    return "▰" * filled + "▱" * (blocks - filled)


def format_seconds(sec: float) -> str:
    sec = int(max(0, sec))
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def pause_playback(player: GuildPlayer) -> bool:
    if player.voice and player.voice.is_playing():
        player.voice.pause()
        player.paused_since = time.monotonic()
        return True
    return False


def resume_playback(player: GuildPlayer) -> bool:
    if player.voice and player.voice.is_paused():
        if player.paused_since is not None:
            player.paused_total += time.monotonic() - player.paused_since
            player.paused_since = None
        player.voice.resume()
        return True
    return False


# ---------------------------------------------------------------- 先読み（次の曲）
PREFETCH_TTL = 3600.0  # 先読みしたストリームURLの有効期限（秒）


async def _prefetch_one(player: GuildPlayer, track: Track):
    url = track.webpage_url
    try:
        stream = await resolve_stream_url(url)
        player.prefetch[url] = (stream, time.monotonic())
    except Exception:
        pass
    finally:
        player.prefetch_pending.discard(url)


def schedule_prefetch(player: GuildPlayer):
    """キューの先頭曲のストリームURLを裏で先読みする（曲間無音の短縮）。"""
    if not player.queue:
        return
    nxt = player.queue[0]
    url = nxt.webpage_url
    if not url or url in player.prefetch or url in player.prefetch_pending:
        return
    player.prefetch_pending.add(url)
    asyncio.create_task(_prefetch_one(player, nxt))


PLAYERS: dict[int, GuildPlayer] = {}


def get_player(guild_id: int) -> GuildPlayer:
    if guild_id not in PLAYERS:
        PLAYERS[guild_id] = GuildPlayer(guild_id)
    return PLAYERS[guild_id]


# ---------------------------------------------------------------- DAVE暗号化の準備待ち
def _can_encrypt(vc) -> bool:
    state = getattr(vc, "_connection", None) if vc else None
    return bool(getattr(state, "can_encrypt", False)) if state is not None else False


async def _wait_encrypt_ready(vc, timeout: float = 10.0) -> bool:
    """DAVEセッションの暗号化準備（MLSハンドシェイク完了）を待つ。

    準備ができる前に再生を始めると、暗号化されていないフレームが
    全クライアントに黙って捨てられ「再生中なのに無音」になる。
    """
    waited = 0.0
    while waited < timeout:
        if _can_encrypt(vc):
            return True
        await asyncio.sleep(0.5)
        waited += 0.5
    ok = _can_encrypt(vc)
    print(f"🔐 DAVE暗号化準備: {'OK' if ok else 'タイムアウト（暗号化可にならなかった）'}")
    return ok


# ---------------------------------------------------------------- VC接続
async def ensure_voice(player: GuildPlayer, member: discord.Member) -> discord.VoiceClient:
    if member.voice is None or member.voice.channel is None:
        raise commands.CommandError("先にボイスチャンネルへ入ってください。")
    channel = member.voice.channel
    player.stopping = False
    player.last_voice_channel_id = channel.id
    try:
        store.set_guild_config(player.guild_id, last_vc_id=channel.id)
    except Exception:
        pass
    if player.voice and player.voice.is_connected():
        if player.voice.channel.id != channel.id:
            await player.voice.move_to(channel)
            await _wait_encrypt_ready(player.voice)
    else:
        player.voice = await channel.connect(self_deaf=True)
        await _wait_encrypt_ready(player.voice)
    return player.voice


# ---------------------------------------------------------------- 再生
async def play_next(bot: commands.Bot, player: GuildPlayer):
    # 再生開始の全行程をロックする。ロックが無いと、逐次追加タスクと
    # 曲終了コールバックが同時にここへ入って二重に voice.play() を呼び、
    # 'Already playing audio.' で落ちる（2026-11-27 実発生）。
    async with player.lock:
        if player.voice is None or not player.voice.is_connected():
            player.current = None
            return
        if player.voice.is_playing() or player.voice.is_paused():
            return  # 既に再生中なら二重起動しない
        cfg = store.guild_config(player.guild_id)
        # --- オートプレイ: キューが尽きたら関連曲を補充して流し続ける
        if not player.queue and cfg.get("autoplay") and player.current is not None:
            seed = player.current
            related = []
            try:
                related = await fetch_related_tracks(
                    seed, seed.requester, player.played_urls, limit=10)
            except Exception:
                related = []
            if related:
                for t in related:
                    player.enqueue(t)
                if player.text_channel_id:
                    channel = bot.get_channel(player.text_channel_id)
                    if channel is not None:
                        try:
                            await channel.send(
                                f"📻 オートプレイ: 関連曲を{len(related)}曲追加しました")
                        except Exception:
                            pass
        track = None
        while player.queue:
            track = player.queue.pop(0)
            player.current = track
            try:
                cached = player.prefetch.get(track.webpage_url)
                if cached and time.monotonic() - cached[1] < PREFETCH_TTL:
                    raw = build_audio_source_from_stream(cached[0], player.filter_key)
                else:
                    raw = await build_audio_source(track.webpage_url, player.filter_key)
            except Exception as e:
                print(f"⚠️ 再生ソース生成に失敗 ({track.title}): {e}")
                player.current = None
                track = None
                continue
            break
        if track is None:
            player.current = None
            player.started_at = 0.0
            player.paused_since = None
            player.paused_total = 0.0
            schedule_idle_disconnect(bot, player)
            await _redraw(bot, player)
            await _update_sub(bot, player, force=True)
            return
        if player.voice is None or not player.voice.is_connected():
            player.current = None
            return
        if not _can_encrypt(player.voice):
            await _wait_encrypt_ready(player.voice, timeout=5.0)
        source = discord.PCMVolumeTransformer(raw, volume=player.volume / 100.0)

        def _after(err, _bot=bot, _player=player):
            if err:
                print(f"⚠️ 再生エラー: {err}")
            asyncio.run_coroutine_threadsafe(advance(_bot, _player), _bot.loop)

        try:
            player.voice.play(source, after=_after)
        except discord.ClientException as e:
            # 万一競合したら曲をキューの先頭へ戻して何もなかったことにする
            print(f"⚠️ 再生開始が競合しました（{e}）。曲をキューへ戻します。")
            player.current = None
            player.queue.insert(0, track)
            return
        # 再生開始: 進捗計測と先読みを開始し、サブ/歌詞パネルを更新する
        player.started_at = time.monotonic()
        player.paused_since = None
        player.paused_total = 0.0
        player.played_urls.add(track.webpage_url)
        schedule_prefetch(player)
        store.push_history(player.guild_id, track.to_dict())
        announced = False
        if cfg.get("announce", True) and player.text_channel_id:
            channel = bot.get_channel(player.text_channel_id)
            if channel is not None:
                try:
                    await channel.send(f"🎵 再生中: **{track.title}**（{track.duration_text()}）")
                    announced = True
                except Exception:
                    pass
        if announced and cfg.get("repost_panel", True):
            # お知らせの直後にパネルを一番下へ置き直す（流れて埋もれないように）
            await _repost(bot, player)
        else:
            await _redraw(bot, player)
        await _update_sub(bot, player, force=True)
        await _update_lyrics(bot, player)


async def advance(bot: commands.Bot, player: GuildPlayer):
    finished = player.current
    if finished and player.loop_mode == "track":
        player.queue.insert(0, finished)
    elif finished and player.loop_mode == "queue":
        player.queue.append(finished)
    await play_next(bot, player)


async def start_if_idle(bot: commands.Bot, player: GuildPlayer):
    """止まっていれば再生を開始し、アイドルタイマーを解除する。"""
    player.cancel_idle()
    if player.voice and player.voice.is_connected() \
            and not player.voice.is_playing() and not player.voice.is_paused() \
            and not player.lock.locked():
        await play_next(bot, player)


# ---------------------------------------------------------------- 音量 / フィルター
def set_volume(player: GuildPlayer, percent: int) -> int:
    """音量を 0-200 に丸めて設定。再生中なら即反映する。"""
    percent = max(0, min(200, int(percent)))
    player.volume = percent
    if player.voice and player.voice.source is not None:
        try:
            player.voice.source.volume = percent / 100.0
        except Exception:
            pass
    return percent


def cycle_loop(player: GuildPlayer) -> str:
    order = ["off", "track", "queue"]
    player.loop_mode = order[(order.index(player.loop_mode) + 1) % len(order)]
    return player.loop_mode


LOOP_TEXT = {"off": "オフ", "track": "この曲をループ", "queue": "キュー全体をループ"}


# ---------------------------------------------------------------- 切断 / 自動退出
async def disconnect_player(bot: commands.Bot, player: GuildPlayer):
    player.stopping = True  # 意図的な切断（VC落ち自動復帰の対象外）
    if player.rejoin_task and not player.rejoin_task.done():
        player.rejoin_task.cancel()
    player.rejoin_task = None
    player.session += 1
    player.cancel_timers()
    player.queue.clear()
    player.current = None
    player.started_at = 0.0
    player.paused_since = None
    player.paused_total = 0.0
    player.prefetch.clear()
    player.played_urls.clear()
    if player.voice and player.voice.is_connected():
        await player.voice.disconnect(force=True)
    player.voice = None
    await _redraw(bot, player)
    await _update_sub(bot, player, force=True)
    await _update_lyrics(bot, player)


# ---------------------------------------------------------------- VC落ち対策（自動復帰）
async def _rejoin_job(bot: commands.Bot, player: GuildPlayer):
    """落ちたVCへ自動で戻り、再生を再開する。最大約10分リトライする。"""
    await asyncio.sleep(8)
    for _ in range(40):
        if player.stopping:
            return
        cfg = store.guild_config(player.guild_id)
        if not cfg.get("stay_247"):
            return
        if player.voice and player.voice.is_connected():
            return
        guild = bot.get_guild(player.guild_id)
        if guild is None:
            return
        channel_id = player.last_voice_channel_id or cfg.get("last_vc_id")
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            return
        try:
            player.voice = await channel.connect(self_deaf=True)
            await _wait_encrypt_ready(player.voice)
            print(f"🔁 VCへ自動再接続しました (guild {player.guild_id})")
            if player.text_channel_id:
                tc = bot.get_channel(player.text_channel_id)
                if tc is not None:
                    try:
                        await tc.send("🔁 VCから落ちたので、自動で再接続しました")
                    except Exception:
                        pass
            await start_if_idle(bot, player)
            return
        except Exception as e:
            print(f"⚠️ VC再接続に失敗（リトライします）: {e}")
            player.voice = None
            await asyncio.sleep(15)


def handle_bot_voice_drop(bot: commands.Bot, player: GuildPlayer):
    """BOT自身が意図せずVCから落ちた時の処理（常駐ON時のみ自動復帰）。"""
    if player.stopping:
        return
    cfg = store.guild_config(player.guild_id)
    # 中断された曲はキューの先頭へ戻して、再接続後に最初から再生する
    if player.current is not None:
        player.queue.insert(0, player.current)
        player.current = None
    player.started_at = 0.0
    player.paused_since = None
    player.paused_total = 0.0
    player.voice = None
    if not cfg.get("stay_247"):
        return
    if player.rejoin_task is None or player.rejoin_task.done():
        player.rejoin_task = asyncio.create_task(_rejoin_job(bot, player))


async def restore_stay_sessions(bot: commands.Bot):
    """起動時: 常駐ONのサーバーは前回のVCへ自動で戻る。

    オートプレイもONなら、最後に再生した曲の関連曲から垂れ流しを再開する。
    """
    for gid, cfg in list(store.data()["guilds"].items()):
        if not cfg.get("stay_247") or not cfg.get("last_vc_id"):
            continue
        guild_id = int(gid)
        guild = bot.get_guild(guild_id)
        if guild is None:
            continue
        channel = guild.get_channel(int(cfg["last_vc_id"]))
        if channel is None:
            continue
        player = get_player(guild_id)
        player.last_voice_channel_id = channel.id
        player.text_channel_id = None
        # パネル設置チャンネルを通知先として使う（あれば）
        for kind in ("panels", "sub_panels", "lyrics_panels"):
            loc = store.panel_loc(kind, guild_id)
            if loc:
                player.text_channel_id = loc["channel"]
                break
        try:
            player.voice = await channel.connect(self_deaf=True)
            await _wait_encrypt_ready(player.voice)
            print(f"🔁 起動時: 常駐VCへ復帰しました (guild {guild_id})")
        except Exception as e:
            print(f"⚠️ 起動時のVC復帰に失敗 (guild {guild_id}): {e}")
            player.voice = None
            continue
        if cfg.get("autoplay") and not player.queue:
            hist = store.history(guild_id, 1)
            if hist:
                seed = Track.from_dict(hist[0], requester="オートプレイ")
                try:
                    related = await fetch_related_tracks(
                        seed, "オートプレイ", set(), limit=10)
                except Exception:
                    related = []
                for t in related:
                    player.enqueue(t)
                if related and player.text_channel_id:
                    tc = bot.get_channel(player.text_channel_id)
                    if tc is not None:
                        try:
                            await tc.send(
                                f"📻 再起動から復帰: 関連曲{len(related)}曲で垂れ流しを再開します")
                        except Exception:
                            pass
        await start_if_idle(bot, player)


def schedule_idle_disconnect(bot: commands.Bot, player: GuildPlayer):
    cfg = store.guild_config(player.guild_id)
    if cfg.get("stay_247"):
        return
    player.cancel_idle()
    seconds = int(cfg.get("idle_seconds", 180))

    async def _job():
        try:
            await asyncio.sleep(seconds)
            if player.voice and player.voice.is_connected() \
                    and not player.voice.is_playing() and not player.voice.is_paused():
                await disconnect_player(bot, player)
        except asyncio.CancelledError:
            pass

    player.idle_task = asyncio.create_task(_job())


def schedule_alone_disconnect(bot: commands.Bot, player: GuildPlayer):
    cfg = store.guild_config(player.guild_id)
    if cfg.get("stay_247"):
        return
    if player.alone_task and not player.alone_task.done():
        return
    seconds = int(cfg.get("alone_seconds", 60))

    async def _job():
        try:
            await asyncio.sleep(seconds)
            await disconnect_player(bot, player)
        except asyncio.CancelledError:
            pass

    player.alone_task = asyncio.create_task(_job())


def cancel_alone_disconnect(player: GuildPlayer):
    if player.alone_task and not player.alone_task.done():
        player.alone_task.cancel()
    player.alone_task = None


# ---------------------------------------------------------------- キュー追加
async def resolve_only(query: str, requester: str,
                       progress=None) -> tuple[list[Track], str]:
    """入力を曲リストへ解決だけする（キューへは入れない）。

    プレイリストのシャッフル確認など、追加前にユーザーへ聞きたい時に使う。
    """
    return await resolve_query(query, requester, progress=progress)


async def add_query(bot: commands.Bot, player: GuildPlayer, query: str,
                    requester: str, text_channel_id: Optional[int] = None,
                    progress=None) -> tuple[list[Track], str]:
    """入力を解決してキューへ追加し、止まっていれば再生を始める。

    プレイリストの扱い（高速化仕様）:
    - プレイリストは確認なしで自動シャッフルする。
    - Spotifyプレイリスト/アルバムは、全曲解決を待たずに
      「1曲ずつ解決してキューへ追加」をバックグラウンドで行い、
      最初の曲が入り次第すぐ再生を始める。
    - YouTubeプレイリスト / SoundCloudセットは一覧取得が1回で済むため、
      シャッフルして一括追加する。
    """
    if text_channel_id:
        player.text_channel_id = text_channel_id

    # --- Spotify プレイリスト / アルバム: 逐次追加（バックグラウンド）
    coll = await get_spotify_collection(query)
    if coll is not None:
        name, queries = coll
        if not queries:
            raise commands.CommandError("Spotifyプレイリストの読み取りに失敗しました。")
        random.shuffle(queries)
        label = f"「{name}」" if name else ""
        session = player.session

        async def fill_rest():
            added = 0
            for q in queries:
                if player.session != session:
                    return  # /停止 などで中断
                try:
                    track, _ = await search_with_fallback(q, requester)
                    track.source = "spotify"
                except Exception:
                    continue
                player.enqueue(track)
                added += 1
                try:
                    await start_if_idle(bot, player)
                except Exception as e:
                    # 1回の再生開始失敗で残り全曲の追加が止まらないようにする
                    print(f"⚠️ 逐次追加中の再生開始に失敗（続行します）: {e}")
                if added % 10 == 0:
                    await _redraw(bot, player)
            if player.session != session:
                return
            await _redraw(bot, player)
            if player.text_channel_id:
                channel = bot.get_channel(player.text_channel_id)
                if channel is not None:
                    try:
                        await channel.send(
                            f"✅ Spotifyプレイリスト{label}の全{added}曲を追加しました（シャッフル済み）")
                    except Exception:
                        pass

        asyncio.create_task(fill_rest())
        return [], (f"📚 Spotifyプレイリスト{label}（{len(queries)}曲）をシャッフルして"
                    "1曲ずつ追加しています。最初の曲から再生します。")

    # --- YouTubeプレイリスト / SoundCloudセット: シャッフルして一括追加
    if is_playlist_query(query):
        tracks, note = await resolve_query(query, requester, progress=progress)
        if len(tracks) > 1:
            random.shuffle(tracks)
            note += "\n（プレイリストをシャッフルして追加しました）"
        for t in tracks:
            player.enqueue(t)
        await start_if_idle(bot, player)
        await _redraw(bot, player)
        return tracks, note

    # --- 通常（単曲 / 検索）
    tracks, note = await resolve_query(query, requester, progress=progress)
    for t in tracks:
        player.enqueue(t)
    await start_if_idle(bot, player)
    await _redraw(bot, player)
    return tracks, note


async def add_tracks(bot: commands.Bot, player: GuildPlayer, tracks: list[Track],
                     text_channel_id: Optional[int] = None,
                     shuffle: bool = False) -> None:
    """既に Track 化された曲（お気に入り/マイリスト等）を追加する。"""
    if text_channel_id:
        player.text_channel_id = text_channel_id
    tracks = list(tracks)
    if shuffle and len(tracks) > 1:
        random.shuffle(tracks)
    for t in tracks:
        player.enqueue(t)
    await start_if_idle(bot, player)
    await _redraw(bot, player)


# ---------------------------------------------------------------- DJロール判定
def can_control(interaction: discord.Interaction) -> bool:
    """DJロール設定時: 管理者 or DJロール所持者のみ操作可。未設定なら全員可。"""
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    cfg = store.guild_config(interaction.guild.id)
    role_id = cfg.get("dj_role_id")
    if not role_id:
        return True
    return any(r.id == int(role_id) for r in interaction.user.roles)
