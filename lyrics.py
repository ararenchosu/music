# -*- coding: utf-8 -*-
"""
lyrics.py — 歌詞取得（LRCLIB: 無料・キー不要）

- 曲タイトルからアーティスト名と曲名を推定して検索する
- ライブ動画対策: ライブ表記を取り除いた候補や「曲名のみ」の候補を
  複数作り、一致度の高い結果を選ぶ
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

# ライブ表記: ここから後ろは曲名ではないことが多いので候補生成時に切り落とす
_LIVE_CUT = re.compile(
    r"[\s\-–—|｜/・]*"
    r"(?:\blive\b|\blive\s*at\b|ライブ|生放送|生中継|ツアー|tour\b|"
    r"\d{4}\s*(?:年|/|\.|-)).*$",
    re.IGNORECASE)
_LIVE_TOKEN = re.compile(r"\blive\b|ライブ", re.IGNORECASE)
_QUOTED = re.compile(r"[「『]([^「『」』]+)[」』]")
_TIMESTAMP = re.compile(r"\[\d{1,2}:\d{2}(?:\.\d+)?\]")


def _clean(text: str) -> str:
    text = _JUNK.sub(" ", text or "")
    text = re.sub(r"\s+", " ", text).strip(" -–—・|｜/")
    return text.strip()


def _live_cut(text: str) -> str:
    """ライブ表記とそれ以降を切り落とし、単独のライブ語も除く。"""
    cut = _LIVE_CUT.sub("", text or "")
    cut = _LIVE_TOKEN.sub(" ", cut)
    return _clean(cut)


def split_artist_title(raw: str) -> tuple[str, str]:
    """'アーティスト - 曲名 (Official Video)' のような題から (歌手, 曲名) を推定。"""
    title = raw or ""
    artist = ""
    song = title
    if " - " in title:
        artist, song = title.split(" - ", 1)
    elif " – " in title:
        artist, song = title.split(" – ", 1)
    song = _clean(song)
    artist = _clean(artist)
    return artist, song or title


def candidates(track: Track) -> list[tuple[str, str]]:
    """検索候補 (アーティスト, 曲名) を優先順に作る。"""
    artist, song = split_artist_title(track.title)
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(a: str, s: str):
        s = (s or "").strip()
        if not s:
            return
        key = (a.strip(), s)
        if key not in seen:
            seen.add(key)
            out.append(key)

    add(artist, song)                      # 通常（歌手＋曲名）
    cut = _live_cut(song)
    if cut != song:
        add(artist, cut)                   # ライブ表記を落とした曲名
        add("", cut)                       # 曲名のみ（ライブ切り）
    add("", song)                          # 曲名のみ
    for q in _QUOTED.findall(track.title or ""):
        add(artist, _clean(q))             # 「曲名」形式の引用部分
        add("", _live_cut(q))
    add("", _live_cut(track.title or ""))  # 最後の手段: 題全体を掃除
    return out


def _norm(text: str) -> str:
    return re.sub(r"[\s\-_・/／.,、。!！?？'\"“”‘’()（）\[\]【】]", "",
                  (text or "").lower())


def _pick(data, want_song: str) -> Optional[str]:
    """検索結果から、曲名の一致度が高い順に歌詞テキストを選ぶ。"""
    if not isinstance(data, list):
        return None
    want = _norm(want_song)
    best: tuple[int, Optional[str]] = (0, None)
    for item in data:
        if not isinstance(item, dict):
            continue
        text = (item.get("plainLyrics") or "").strip()
        if not text:
            synced = (item.get("syncedLyrics") or "").strip()
            if synced:
                text = _TIMESTAMP.sub("", synced).strip()
        if not text:
            continue
        name = _norm(str(item.get("trackName") or ""))
        if want and name == want:
            score = 3
        elif want and name and (name in want or want in name):
            score = 2
        else:
            score = 1
        if score > best[0]:
            best = (score, text)
            if score == 3:
                break
    return best[1]


async def _search(session: aiohttp.ClientSession, params: dict,
                  want_song: str) -> Optional[str]:
    try:
        async with session.get(f"{LRCLIB}/search", params=params,
                               timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception:
        return None
    return _pick(data, want_song)


async def fetch_lyrics(track: Track) -> Optional[str]:
    """曲の歌詞を返す。見つからなければ None。結果はキャッシュする。"""
    key = track.webpage_url or track.title
    if key in _cache:
        return _cache[key]
    result: Optional[str] = None
    try:
        async with aiohttp.ClientSession(
                headers={"User-Agent": "SuperMusicBot/1.0 (lyrics lookup)"}) as session:
            for artist, song in candidates(track):
                params = {"track_name": song}
                if artist:
                    params["artist_name"] = artist
                result = await _search(session, params, song)
                if result:
                    break
            if not result:
                # フリーテキスト検索でもう一押し（曲名のみ）
                _, song0 = split_artist_title(track.title)
                result = await _search(session, {"q": _live_cut(song0)},
                                       _live_cut(song0))
    except Exception:
        result = None
    if result and len(result) > MAX_CHARS:
        result = result[:MAX_CHARS] + "\n…（以下省略）"
    _cache[key] = result
    return result


def peek_cache(track: Track) -> Optional[str]:
    """キャッシュ済みの歌詞だけを見る（未取得なら None）。"""
    return _cache.get(track.webpage_url or track.title)


def _lyrics_text(item: dict) -> str:
    """検索結果1件から歌詞テキストを取り出す（同期歌詞はタグを外す）。"""
    text = (item.get("plainLyrics") or "").strip()
    if not text:
        synced = (item.get("syncedLyrics") or "").strip()
        if synced:
            text = _TIMESTAMP.sub("", synced).strip()
    return text


async def _search_raw(session: aiohttp.ClientSession, params: dict) -> list:
    try:
        async with session.get(f"{LRCLIB}/search", params=params,
                               timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
    except Exception:
        return []
    return data if isinstance(data, list) else []


async def search_lyrics_candidates(artist: str, song: str,
                                   limit: int = 10) -> list[dict]:
    """作者＋曲名で検索し、歌詞のある候補を複数返す。

    戻り値: [{"track","artist","album","duration","text"}, ...]
    """
    artist = (artist or "").strip()
    song = (song or "").strip()
    if not song:
        return []
    out: list[dict] = []
    seen: set[tuple[str, str, int]] = set()
    try:
        async with aiohttp.ClientSession(
                headers={"User-Agent": "SuperMusicBot/1.0 (lyrics lookup)"}) as session:
            queries: list[dict] = []
            if artist:
                queries.append({"track_name": song, "artist_name": artist})
            queries.append({"track_name": song})
            for params in queries:
                for item in await _search_raw(session, params):
                    if not isinstance(item, dict) or item.get("instrumental"):
                        continue
                    text = _lyrics_text(item)
                    if not text:
                        continue
                    dur = int(item.get("duration") or 0)
                    key = (_norm(str(item.get("trackName") or "")),
                           _norm(str(item.get("artistName") or "")), dur)
                    if key in seen:
                        continue
                    seen.add(key)
                    if len(text) > MAX_CHARS:
                        text = text[:MAX_CHARS] + "\n…（以下省略）"
                    out.append({
                        "track": str(item.get("trackName") or song),
                        "artist": str(item.get("artistName") or artist),
                        "album": str(item.get("albumName") or ""),
                        "duration": dur,
                        "text": text,
                    })
                    if len(out) >= limit:
                        return out
    except Exception:
        return out
    return out


async def search_lyrics_manual(artist: str, song: str) -> Optional[str]:
    """作者＋曲名で手動検索する。見つからなければ None。"""
    artist = (artist or "").strip()
    song = (song or "").strip()
    if not song:
        return None
    result: Optional[str] = None
    try:
        async with aiohttp.ClientSession(
                headers={"User-Agent": "SuperMusicBot/1.0 (lyrics lookup)"}) as session:
            attempts: list[dict] = []
            if artist:
                attempts.append({"track_name": song, "artist_name": artist})
            attempts.append({"track_name": song})
            attempts.append({"q": f"{artist} {song}".strip()})
            for params in attempts:
                result = await _search(session, params, song)
                if result:
                    break
    except Exception:
        result = None
    if result and len(result) > MAX_CHARS:
        result = result[:MAX_CHARS] + "\n…（以下省略）"
    return result


def set_override(track: Track, text: Optional[str]):
    """その曲の歌詞を手動結果で上書きする（次の曲では通常の自動取得に戻る）。"""
    _cache[track.webpage_url or track.title] = text
