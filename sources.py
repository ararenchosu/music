# -*- coding: utf-8 -*-
"""
sources.py — 音源の解決（yt-dlp）

対応:
- YouTube: 検索 / 動画URL / プレイリストURL
- SoundCloud: 検索 / URL
- Spotify: spotify.py で検索語に変換してから下記で解決

フォールバック設計:
- YouTube の検索・取得が失敗したら、同じ検索語で SoundCloud を検索し直す。
  無料ホストでは YouTube 側のIPブロックが起きやすいため、その場合でも
  SoundCloud の曲なら再生を続けられるようにするための保険。
  （URLを直接指定された場合はそのURLを尊重し、フォールバックしない）
"""

import asyncio
from typing import Optional

import discord
import yt_dlp

import spotify as spotify_mod
from core import (Track, build_ydl_opts, make_track,
                  FFMPEG, FFMPEG_BEFORE_OPTS, FILTERS)

BUILD_TAG = "super-music-20261127"


# ---------------------------------------------------------------- yt-dlp 実行
async def _extract(query: str, opts: dict) -> dict:
    loop = asyncio.get_running_loop()

    def _run():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(query, download=False)

    return await loop.run_in_executor(None, _run)


async def ytdl_extract(query: str, *, flat: bool = False,
                       any_format: bool = False,
                       source: str = "youtube") -> dict:
    opts = build_ydl_opts(flat=flat, no_format=any_format, source=source)
    try:
        return await _extract(query, opts)
    except Exception as e:
        # フォーマット指定が合わない場合は緩めて1回だけ再試行
        if "format is not available" in str(e).lower() and not flat and not any_format:
            loose = build_ydl_opts(flat=flat, source=source)
            loose["format"] = "bestaudio/best/worst"
            return await _extract(query, loose)
        raise


def _first_entry(info: dict) -> dict:
    if "entries" in info and info["entries"]:
        return info["entries"][0]
    return info


def is_playlist_query(query: str) -> bool:
    """入力がプレイリスト系リンクかどうか（シャッフル確認の表示判定用）。"""
    q = query.strip()
    if spotify_mod.COLLECTION_RE.search(q):
        return True
    if q.startswith(("http://", "https://")):
        if "youtube.com" in q and "list=" in q:
            return True
        if "soundcloud.com" in q and "/sets/" in q:
            return True
    return False


# ---------------------------------------------------------------- 単曲の解決
async def search_youtube(query: str, requester: str) -> Track:
    info = await ytdl_extract(f"ytsearch1:{query}")
    track = make_track(_first_entry(info), requester, source="youtube")
    if not track.webpage_url:
        raise RuntimeError("YouTubeで曲が見つかりませんでした")
    return track


async def search_soundcloud(query: str, requester: str) -> Track:
    info = await ytdl_extract(f"scsearch1:{query}", source="soundcloud")
    track = make_track(_first_entry(info), requester, source="soundcloud")
    if not track.webpage_url:
        raise RuntimeError("SoundCloudで曲が見つかりませんでした")
    return track


async def search_with_fallback(query: str, requester: str) -> tuple[Track, str]:
    """検索語 -> 曲。YouTube失敗時は SoundCloud へ自動フォールバックする。

    戻り値: (Track, 補足メッセージ)
    """
    try:
        return await search_youtube(query, requester), ""
    except Exception:
        track = await search_soundcloud(query, requester)
        return track, "（YouTubeで取得できなかったため SoundCloud から再生します）"


async def extract_url(url: str, requester: str) -> Track:
    source = "soundcloud" if "soundcloud.com" in url else "youtube"
    info = await ytdl_extract(url, source=source)
    track = make_track(_first_entry(info), requester, source=source)
    if not track.webpage_url:
        raise RuntimeError("曲の情報を取得できませんでした")
    return track


# ---------------------------------------------------------------- 入力全体の解決
async def resolve_query(query: str, requester: str,
                        progress=None) -> tuple[list[Track], str]:
    """ユーザー入力（検索語 / 各種URL / Spotifyリンク）を曲リストへ解決する。

    progress: async callable(done, total) — プレイリスト追加の進捗通知用（任意）
    戻り値: (追加する曲リスト, 補足メッセージ)
    Spotifyコレクションは「最初の1曲を即返し、残りは呼び出し側で背景追加」ではなく、
    ここで全曲を順に解決して返す（進捗は progress で通知）。
    """
    query = query.strip()
    note = ""

    # --- Spotify 曲リンク
    if spotify_mod.TRACK_RE.search(query):
        search_text = await spotify_mod.resolve_track_query(query)
        if not search_text:
            raise RuntimeError("Spotifyリンクの読み取りに失敗しました")
        track, fb = await search_with_fallback(search_text, requester)
        track.source = "spotify"
        return [track], ("Spotifyリンクから検索しました。" + fb)

    # --- Spotify プレイリスト / アルバム
    if spotify_mod.COLLECTION_RE.search(query):
        name, queries = await spotify_mod.resolve_collection_queries(query)
        if not queries:
            raise RuntimeError("Spotifyプレイリストの読み取りに失敗しました")
        label = f"「{name}」" if name else ""
        tracks: list[Track] = []
        for i, q in enumerate(queries):
            try:
                track, _ = await search_with_fallback(q, requester)
                track.source = "spotify"
                tracks.append(track)
            except Exception:
                pass
            if progress:
                await progress(i + 1, len(queries))
        if not tracks:
            raise RuntimeError("プレイリストの曲が1曲も見つかりませんでした")
        return tracks, f"Spotify{label}から{len(tracks)}曲を追加しました。"

    # --- URL
    if query.startswith(("http://", "https://")):
        # YouTubeプレイリスト
        if "youtube.com" in query and "list=" in query:
            info = await ytdl_extract(query, flat=True)
            tracks = []
            for entry in (info.get("entries") or []):
                if entry and entry.get("id"):
                    t = make_track(entry, requester, source="youtube")
                    if t.webpage_url:
                        tracks.append(t)
            if not tracks:
                raise RuntimeError("プレイリストが空か読み取れませんでした")
            return tracks, f"YouTubeプレイリストから{len(tracks)}曲を追加しました。"
        # SoundCloud セット（プレイリスト）
        if "soundcloud.com" in query and "/sets/" in query:
            info = await ytdl_extract(query, flat=True, source="soundcloud")
            tracks = []
            for entry in (info.get("entries") or []):
                if entry and entry.get("id"):
                    t = make_track(entry, requester, source="soundcloud")
                    t.webpage_url = entry.get("url") or t.webpage_url
                    if t.webpage_url:
                        tracks.append(t)
            if tracks:
                return tracks, f"SoundCloudセットから{len(tracks)}曲を追加しました。"
        track = await extract_url(query, requester)
        return [track], note

    # --- 検索語（YouTube -> SoundCloud フォールバック）
    track, fb = await search_with_fallback(query, requester)
    return [track], fb


# ---------------------------------------------------------------- 再生ソース生成
async def resolve_stream_url(webpage_url: str) -> str:
    """再生直前にストリームURLを取り直す（待機中のURL失効を避ける）。"""
    source = "soundcloud" if "soundcloud.com" in webpage_url else "youtube"
    info = await ytdl_extract(webpage_url, source=source)
    url = info.get("url")
    if not url:
        raise RuntimeError("ストリームURLを取得できませんでした")
    return url


async def build_audio_source(webpage_url: str, filter_key: str = "none") -> discord.AudioSource:
    """ストリームURL + FFmpegフィルターから再生ソースを作る（音量は付けない）。

    音量は player 側で PCMVolumeTransformer を被せて制御する。
    """
    stream = await resolve_stream_url(webpage_url)
    options = "-vn"
    af = FILTERS.get(filter_key, FILTERS["none"])["af"]
    if af:
        options = f"-vn -af {af}"
    return discord.FFmpegPCMAudio(
        stream, executable=FFMPEG,
        before_options=FFMPEG_BEFORE_OPTS, options=options)


# ---------------------------------------------------------------- 診断用
async def extract_raw_youtube(url: str) -> dict:
    """フォーマット選択を介さず生の動画情報を取る（/診断 用）。"""
    loop = asyncio.get_running_loop()
    opts = build_ydl_opts(no_format=True)

    def _run():
        with yt_dlp.YoutubeDL(opts) as ydl:
            ie = ydl.get_info_extractor("Youtube")
            return ie.extract(url)

    return await loop.run_in_executor(None, _run)


def summarize_formats(formats: list) -> dict:
    audio_only = [f for f in formats
                  if f.get("acodec") not in (None, "none")
                  and f.get("vcodec") in (None, "none")]
    playable = [f for f in formats if f.get("acodec") not in (None, "none")]
    storyboards = [f for f in formats
                   if str(f.get("format_id", "")).startswith("sb")
                   or f.get("ext") == "mhtml"]
    return {"total": len(formats), "audio_only": len(audio_only),
            "playable": len(playable), "storyboards": len(storyboards)}


async def check_soundcloud() -> tuple[bool, str]:
    """/診断 用: SoundCloud 検索が通るか軽く確認する。"""
    try:
        info = await ytdl_extract("scsearch1:lofi", source="soundcloud")
        entry = _first_entry(info)
        if entry and entry.get("title"):
            return True, f"検索OK（例: {entry['title'][:40]}）"
        return False, "検索結果が空でした"
    except Exception as e:
        return False, f"検索失敗: {str(e)[:120]}"
