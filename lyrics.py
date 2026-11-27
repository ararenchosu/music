# -*- coding: utf-8 -*-
"""
lyrics.py — 歌詞取得（LRCLIB: 無料・キー不要）

- 曲タイトルからアーティスト名と曲名を推定して検索する
- 取得結果はメモリにキャッシュする（同じ曲の再取得を避ける）
- 見つからない場合は None
"""

import re
from typing import Optional

import aiohttp

from core import Track

LRCLIB = "https://lrclib.net/api"
MAX_CHARS = 3500

_cache: dict[str, Optional[str]] = {}

_JUNK = re.compile(
    r"[（(【\[][^）)】\]]*[）)】\]]"
    r"|official|music\s*video|\bmv\b|\bpv\b|lyrics?\s*video|audio|video"
    r"|フル|歌詞付き|字幕",
    re.IGNORECASE)


def split_artist_title(raw: str) -> tuple[str, str]:
    """'アーティスト - 曲名 (Official Video)' のような題から (歌手, 曲名) を推定。"""
    title = raw or ""
    artist = ""
    song = title
    if " - " in title:
        artist, song = title.split(" - ", 1)
    elif " – " in title:
        artist, song = title.split(" – ", 1)
    song = _JUNK.sub(" ", song).strip(" -–・")
    artist = _JUNK.sub(" ", artist).strip(" -–・")
    song = re.sub(r"\s+", " ", song).strip()
    artist = re.sub(r"\s+", " ", artist).strip()
    return artist, song or title


async def _search(session: aiohttp.ClientSession, params: dict) -> Optional[str]:
    try:
        async with session.get(f"{LRCLIB}/search", params=params,
                               timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception:
        return None
    if isinstance(data, list):
        for item in data:
            text = (item or {}).get("plainLyrics")
            if text and text.strip():
                return text.strip()
    return None


async def fetch_lyrics(track: Track) -> Optional[str]:
    """曲の歌詞を返す。見つからなければ None。結果はキャッシュする。"""
    key = track.webpage_url or track.title
    if key in _cache:
        return _cache[key]
    artist, song = split_artist_title(track.title)
    result: Optional[str] = None
    try:
        async with aiohttp.ClientSession(
                headers={"User-Agent": "SuperMusicBot/1.0 (lyrics lookup)"}) as session:
            if artist and song:
                result = await _search(session, {"track_name": song, "artist_name": artist})
            if not result and song:
                result = await _search(session, {"track_name": song})
            if not result:
                result = await _search(session, {"track_name": track.title})
    except Exception:
        result = None
    if result and len(result) > MAX_CHARS:
        result = result[:MAX_CHARS] + "\n…（以下省略）"
    _cache[key] = result
    return result


def peek_cache(track: Track) -> Optional[str]:
    """キャッシュ済みの歌詞だけを見る（未取得なら None）。"""
    return _cache.get(track.webpage_url or track.title)
