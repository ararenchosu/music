# -*- coding: utf-8 -*-
"""
spotify.py — Spotify リンクの解決（APIキー不要・埋め込みページ解析）

Spotify は直接再生できないため、埋め込みページから曲名+アーティストを取り出し、
検索語として返す。実際の再生は sources.py が YouTube / SoundCloud で行う。

- 曲リンク        -> 検索語（1つ）
- プレイリスト/アルバム -> (名前, [検索語...])
"""

import json
import re
from typing import Optional

import aiohttp

TRACK_RE = re.compile(r"open\.spotify\.com/(?:intl-[a-z-]+/)?track/([A-Za-z0-9]+)")
COLLECTION_RE = re.compile(
    r"open\.spotify\.com/(?:intl-[a-z-]+/)?(playlist|album)/([A-Za-z0-9]+)")
_HEADERS = {"User-Agent": "Mozilla/5.0"}


def is_spotify_url(text: str) -> bool:
    return "open.spotify.com" in text


async def _fetch_embed(kind: str, spotify_id: str) -> Optional[str]:
    url = f"https://open.spotify.com/embed/{kind}/{spotify_id}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=_HEADERS,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return None
                return await resp.text()
    except Exception:
        return None


async def resolve_track_query(url: str) -> Optional[str]:
    """曲リンク -> 'アーティスト 曲名' の検索語。失敗時は None。"""
    m = TRACK_RE.search(url)
    if not m:
        return None
    html = await _fetch_embed("track", m.group(1))
    if not html:
        return None
    name_match = re.search(r'"name":"([^"]+)","uri":"spotify:track:', html)
    if name_match:
        title = name_match.group(1)
        artists = re.findall(r'"artists":\[\{"name":"([^"]+)"', html)
        return f"{artists[0]} {title}".strip() if artists else title
    names = re.findall(r'"name":"(.*?)"', html)
    if not names:
        return None
    title = names[0].replace('\\"', '"')
    artist = names[1].replace('\\"', '"') if len(names) > 1 else ""
    return f"{title} {artist}".strip()


async def resolve_collection_queries(url: str) -> tuple[Optional[str], list[str]]:
    """プレイリスト/アルバム -> (名前, [検索語...])。

    埋め込みページの __NEXT_DATA__ -> entity.trackList
    （title=曲名, subtitle=アーティスト名）を読む。取れなければ正規表現で拾う。
    """
    m = COLLECTION_RE.search(url)
    if not m:
        return None, []
    kind, spotify_id = m.group(1), m.group(2)
    html = await _fetch_embed(kind, spotify_id)
    if not html:
        return None, []
    name: Optional[str] = None
    queries: list[str] = []
    m_data = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html, re.S)
    if m_data:
        try:
            payload = json.loads(m_data.group(1))
            entity = payload["props"]["pageProps"]["state"]["data"]["entity"]
            name = entity.get("name") or entity.get("title")
            for t in entity.get("trackList", []):
                title = (t.get("title") or "").replace("\xa0", " ").strip()
                artist = (t.get("subtitle") or "").replace("\xa0", " ").strip()
                if title:
                    queries.append(f"{artist} {title}".strip())
        except Exception as e:
            print(f"⚠️ SpotifyコレクションJSON解析失敗: {e}")
    if not queries:
        for title, artist in re.findall(
                r'"title":"([^"]+)","subtitle":"([^"]+)"', html):
            queries.append(f"{artist} {title}".strip())
        if not name:
            nm = re.search(
                r'"entity":\{"type":"(?:playlist|album)","name":"([^"]+)"', html)
            if nm:
                name = nm.group(1)
    return name, queries
