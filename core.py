# -*- coding: utf-8 -*-
"""
core.py — 超高機能音楽BOT の共通基盤（新規設計）

- トークンは環境変数 MUSIC_DISCORD_TOKEN（.env 対応）
- FFmpeg の解決、YouTube クッキーの準備、yt-dlp オプション生成
- 曲データ Track、フィルター定義、BOT 本体
"""

import base64
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

TOKEN = os.getenv("MUSIC_DISCORD_TOKEN", "").strip()

MAX_QUEUE_SHOW = 10

# ---------------------------------------------------------------- フィルター
# FFmpeg の -af に渡すプリセット。スロー+リバーブは aecho で近似する。
FILTERS: dict[str, dict] = {
    "none":       {"label": "なし",           "af": ""},
    "bassboost":  {"label": "低音ブースト",   "af": "bass=g=15:f=110:w=0.3"},
    "nightcore":  {"label": "ナイトコア",     "af": "asetrate=44100*1.25,aresample=44100"},
    "slowreverb": {"label": "スロー+リバーブ", "af": "atempo=0.85,aecho=0.8:0.88:60:0.35"},
}
FILTER_ORDER = ["none", "bassboost", "nightcore", "slowreverb"]


def filter_label(key: str) -> str:
    return FILTERS.get(key, FILTERS["none"])["label"]


# ---------------------------------------------------------------- FFmpeg 解決
def resolve_ffmpeg() -> str:
    """システムの ffmpeg → imageio-ffmpeg 同梱バイナリ の順で探す。"""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import importlib.util
        spec = importlib.util.find_spec("imageio_ffmpeg")
        if spec and spec.submodule_search_locations:
            binaries = Path(list(spec.submodule_search_locations)[0]) / "binaries"
            for cand in sorted(binaries.glob("ffmpeg-*")):
                try:
                    os.chmod(cand, 0o755)
                except Exception:
                    pass
                return str(cand)
    except Exception:
        pass
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


FFMPEG = resolve_ffmpeg()
FFMPEG_BEFORE_OPTS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"

# ---------------------------------------------------------------- クッキー
def prepare_cookie_file() -> Optional[str]:
    """優先順位: 同フォルダの cookies.txt / YT_COOKIES_FILE / YT_COOKIES_B64。"""
    local = BASE_DIR / "cookies.txt"
    if local.exists() and local.stat().st_size > 0:
        return str(local)
    plain = os.getenv("YT_COOKIES_FILE", "").strip()
    if plain and Path(plain).exists():
        return plain
    b64 = os.getenv("YT_COOKIES_B64", "").strip()
    if not b64:
        return None
    try:
        raw = base64.b64decode(b64)
        path = Path(tempfile.gettempdir()) / "yt_cookies_super.txt"
        path.write_bytes(raw)
        return str(path)
    except Exception as e:
        print(f"⚠️ YT_COOKIES_B64 のデコードに失敗: {e}")
        return None


COOKIE_FILE = prepare_cookie_file()


def build_ydl_opts(*, flat: bool = False, no_format: bool = False,
                   source: str = "youtube") -> dict:
    """yt-dlp オプション。source='soundcloud' の時はクッキーを付けない。"""
    opts = {
        "format": "bestaudio[ext=webm]/bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 20,
        "retries": 3,
    }
    if no_format:
        opts.pop("format", None)
    if flat:
        opts["extract_flat"] = "in_playlist"
        opts["noplaylist"] = False
    if source == "youtube" and COOKIE_FILE:
        opts["cookiefile"] = COOKIE_FILE
    return opts


# ---------------------------------------------------------------- 曲データ
@dataclass
class Track:
    title: str
    webpage_url: str
    duration: Optional[int] = None
    requester: str = ""
    source: str = "youtube"   # youtube / soundcloud / spotify(解決元)

    def duration_text(self) -> str:
        if not self.duration:
            return "--:--"
        m, s = divmod(int(self.duration), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    def to_dict(self) -> dict:
        return {"title": self.title, "webpage_url": self.webpage_url,
                "duration": self.duration, "source": self.source}

    @staticmethod
    def from_dict(d: dict, requester: str = "") -> "Track":
        return Track(title=d.get("title") or "（タイトル不明）",
                     webpage_url=d.get("webpage_url") or "",
                     duration=d.get("duration"),
                     requester=requester,
                     source=d.get("source") or "youtube")


def make_track(info: dict, requester: str, source: str = "youtube") -> Track:
    vid = info.get("id")
    url = info.get("webpage_url") or ""
    if not url and vid and source == "youtube":
        url = f"https://www.youtube.com/watch?v={vid}"
    return Track(title=info.get("title") or "（タイトル不明）",
                 webpage_url=url,
                 duration=info.get("duration"),
                 requester=requester,
                 source=source)


# ---------------------------------------------------------------- BOT 本体
class SuperMusicBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # import はここで遅延（panel -> player -> core の循環を避ける）
        import store
        from panel import MusicPanelView
        store.load_data()
        self.add_view(MusicPanelView(self))
        await self.tree.sync()

    async def on_ready(self):
        print(f"✅ ログインしました: {self.user} (ID: {self.user.id})")
        print(f"🎬 FFmpeg: {FFMPEG}")
        print(f"🍪 クッキー: {'使用中' if COOKIE_FILE else 'なし'}")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening, name="/再生 で音楽再生"))
        # 設置済みパネルを最新の見た目に自動再描画する
        from panel import redraw_all_panels
        await redraw_all_panels(self)


bot = SuperMusicBot()
