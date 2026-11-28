# -*- coding: utf-8 -*-
"""
spotify.py — Spotify リンクの解決（APIキー不要・埋め込みページ解析）

Spotify は直接再生できないため、埋め込みページから曲名+アーティストを取り出し、
検索語として返す。実際の再生は sources.py が YouTube / SoundCloud で行う。

- 曲リンク        -> 検索語（1つ）
- プレイリスト/アルバム -> (名前, [検索語...])
"""

import base64
import json
import os
import re
from typing import Optional

import asyncio

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
                    print(f"[spotify] トークン取得失敗 status={resp.status}")
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
    except Exception as e:
        print(f"[spotify] トークン取得エラー: {type(e).__name__}")
        return None


_app_token_cache: tuple[str, float] = ("", 0.0)


async def _app_token() -> Optional[str]:
    """公式アプリ認証（Client Credentials）のトークン。

    環境変数 SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET が設定されている
    場合のみ使う。これが一番確実な取得方法。
    """
    global _app_token_cache
    cid = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()
    if not cid or not secret:
        return None
    token, expires = _app_token_cache
    if token and _time.time() < expires - 60:
        return token
    basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                    "https://accounts.spotify.com/api/token",
                    headers={"Authorization": f"Basic {basic}",
                             "Content-Type": "application/x-www-form-urlencoded"},
                    data={"grant_type": "client_credentials"},
                    timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    print(f"[spotify] アプリ認証失敗 status={resp.status}"
                          "（ID/シークレットを確認してください）")
                    return None
                data = await resp.json()
    except Exception as e:
        print(f"[spotify] アプリ認証エラー: {type(e).__name__}")
        return None
    token = data.get("access_token")
    if not token:
        return None
    _app_token_cache = (token, _time.time() + int(data.get("expires_in", 3600)))
    print("[spotify] 公式アプリ認証のトークンを使用します")
    return token


async def _embed_token(kind: str, spotify_id: str) -> Optional[str]:
    """埋め込みページ内に書かれているアクセストークンを取り出す（予備手段）。"""
    html = await _fetch_embed(kind, spotify_id)
    if not html:
        print("[spotify] 埋め込みページ取得に失敗（トークン予備手段も不可）")
        return None
    m = re.search(r'"accessToken"\s*:\s*"([^"]+)"', html)
    if not m:
        print("[spotify] 埋め込みページ内にトークンが見つかりません")
        return None
    print("[spotify] 埋め込みページのトークンを使用します")
    return m.group(1)


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
    """公式APIをページングして (名前, [検索語...全曲]) を返す。失敗時は (None, [])。

    - ページ失敗時は再試行（429は長めに待つ、401はトークン更新）
    - 途中で詰まったら、もう一方のトークン（Web/埋め込み）に切り替えて
      止まった位置から再開する
    - 取得状況は print でログに残す（Termuxで確認できる）
    """
    global _token_cache
    limit = 100 if kind == "playlist" else 50
    base = f"https://api.spotify.com/v1/{kind}s/{spotify_id}"
    name: Optional[str] = None
    queries: list[str] = []
    total: Optional[int] = None
    offset = 0

    sources: list[tuple[str, Optional[str]]] = []
    app = await _app_token()
    if app:
        sources.append(("app", app))
    web = await _web_token()
    if web:
        sources.append(("web", web))
    sources.append(("embed", None))  # 必要になったら取得する

    try:
        async with aiohttp.ClientSession() as session:
            for src_name, token in sources:
                if token is None:
                    token = await _embed_token(kind, spotify_id)
                    if not token:
                        continue
                refreshed = False
                while True:
                    # メタ情報（名前）は最初の成功時に取る
                    if name is None:
                        try:
                            async with session.get(
                                    base,
                                    headers={"Authorization": f"Bearer {token}",
                                             **_HEADERS},
                                    timeout=aiohttp.ClientTimeout(total=15)) as resp:
                                if resp.status == 200:
                                    meta = await resp.json()
                                    name = meta.get("name")
                        except Exception:
                            pass
                    page_url = f"{base}/tracks?limit={limit}&offset={offset}"
                    data = None
                    for attempt in range(4):
                        try:
                            async with session.get(
                                    page_url,
                                    headers={"Authorization":
                                             f"Bearer {token}", **_HEADERS},
                                    timeout=aiohttp.ClientTimeout(total=20)) as resp:
                                if resp.status == 200:
                                    data = await resp.json()
                                    break
                                print(f"[spotify] ページ取得失敗 status={resp.status}"
                                      f" offset={offset} token={src_name}"
                                      f" 試行{attempt + 1}/4")
                                if resp.status == 401 and not refreshed \
                                        and src_name in ("web", "app"):
                                    refreshed = True
                                    if src_name == "web":
                                        _token_cache = ("", 0.0)
                                        token = await _web_token()
                                    else:
                                        globals()["_app_token_cache"] = ("", 0.0)
                                        token = await _app_token()
                                    if token:
                                        continue
                                if resp.status == 429:
                                    await asyncio.sleep(5)
                                    continue
                        except Exception as e:
                            print(f"[spotify] ページ取得エラー offset={offset}"
                                  f" token={src_name} 試行{attempt + 1}/4:"
                                  f" {type(e).__name__}")
                        await asyncio.sleep(1.5)
                    if data is None:
                        print(f"[spotify] ページ取得を断念 offset={offset}"
                              f" token={src_name}（{len(queries)}曲まで取得）")
                        break  # 次のトークンで再開を試す
                    items = data.get("items") or []
                    if total is None and data.get("total") is not None:
                        total = int(data["total"])
                    if not items:
                        offset = total if total is not None else offset
                        break
                    for item in items:
                        obj = item.get("track") if kind == "playlist" else item
                        q = _query_of(obj)
                        if q:
                            queries.append(q)
                    offset += len(items)
                    if total is not None and offset >= total:
                        break
                    if len(items) < limit:
                        break
                if total is not None and offset >= total:
                    break  # 全曲取れた
                if total is None and queries:
                    break  # 終端まで来たとみなす
                print(f"[spotify] トークンを切り替えて再開します offset={offset}")
    except Exception as e:
        print(f"[spotify] API取得エラー: {type(e).__name__}（{len(queries)}曲で中断）")
    print(f"[spotify] API取得結果: {len(queries)}曲 / 全{total}曲")
    return (name, queries) if queries else (None, [])


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
        print(f"[spotify] 埋め込み取得結果: {len(emb_queries)}曲")
        if len(emb_queries) > len(api_queries):
            return emb_name, emb_queries
        return api_name or emb_name, api_queries
    name, queries = await _resolve_collection_embed(url)
    print(f"[spotify] 埋め込み取得結果: {len(queries)}曲")
    return name, queries
