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


async def _resolve_collection_embed(url: str) -> tuple[Optional[str], list[str]]:
    """埋め込みページから取る方法（曲数に上限があるため予備扱い）。"""
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


# ---------------------------------------------------------------- 全曲取得（Web API・匿名トークン）
# 埋め込みページの trackList は大きいプレイリストだと途中で切れるため、
# Webプレイヤー用の匿名トークンで公式APIをページングして全曲を取る。
# APIが使えない環境では埋め込み方式にフォールバックする。
import time as _time

_token_cache: tuple[str, float] = ("", 0.0)


async def _web_token() -> Optional[str]:
    global _token_cache
    token, expires = _token_cache
    if token and _time.time() < expires - 60:
        return token
    url = ("https://open.spotify.com/get_access_token"
           "?reason=transport&productType=web_player")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=_HEADERS,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        token = data.get("accessToken")
        if not token:
            return None
        ttl = int(data.get("accessTokenExpirationTimestampMs", 0) // 1000
                  - _time.time()) if data.get(
                      "accessTokenExpirationTimestampMs") else 3000
        _token_cache = (token, _time.time() + max(600, ttl))
        return token
    except Exception:
        return None


def _query_of(track_obj) -> Optional[str]:
    if not track_obj or track_obj.get("type") not in (None, "track"):
        return None
    title = (track_obj.get("name") or "").strip()
    if not title:
        return None
    artists = track_obj.get("artists") or []
    artist = (artists[0].get("name") or "").strip() if artists else ""
    return f"{artist} {title}".strip()


async def _fetch_collection_api(kind: str, spotify_id: str):
    """公式APIをページングして (名前, [検索語...全曲]) を返す。失敗時は (None, [])。"""
    token = await _web_token()
    if not token:
        return None, []
    headers = {"Authorization": f"Bearer {token}", **_HEADERS}
    limit = 100 if kind == "playlist" else 50
    base = f"https://api.spotify.com/v1/{kind}s/{spotify_id}"
    name: Optional[str] = None
    queries: list[str] = []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(base, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    meta = await resp.json()
                    name = meta.get("name")
            offset = 0
            while True:
                page_url = f"{base}/tracks?limit={limit}&offset={offset}"
                async with session.get(page_url, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status != 200:
                        return (name, queries) if queries else (None, [])
                    data = await resp.json()
                items = data.get("items") or []
                if not items:
                    break
                for item in items:
                    obj = item.get("track") if kind == "playlist" else item
                    q = _query_of(obj)
                    if q:
                        queries.append(q)
                offset += len(items)
                total = data.get("total")
                if total is not None and offset >= int(total):
                    break
                if len(items) < limit:
                    break
    except Exception:
        return (name, queries) if queries else (None, [])
    return name, queries


async def resolve_collection_queries(url: str) -> tuple[Optional[str], list[str]]:
    """プレイリスト/アルバム -> (名前, [検索語...])。全曲取得を優先する。"""
    m = COLLECTION_RE.search(url)
    if not m:
        return None, []
    kind, spotify_id = m.group(1), m.group(2)
    api_name, api_queries = await _fetch_collection_api(kind, spotify_id)
    if api_queries:
        emb_name, emb_queries = None, []
        # APIで取れた数が埋め込み以上ならAPIを採用（通常はこちらが全曲）
        if len(api_queries) >= 100:
            return api_name, api_queries
        emb_name, emb_queries = await _resolve_collection_embed(url)
        if len(emb_queries) > len(api_queries):
            return emb_name, emb_queries
        return api_name or emb_name, api_queries
    return await _resolve_collection_embed(url)
