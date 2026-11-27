# -*- coding: utf-8 -*-
"""
core.py — VC常駐読み上げBOT v2 の共通基盤

- 環境変数 / .env 読み込み、BOT本体と唯一のインスタンス bot
- サーバーごとの設定ストア（tts_config.json に保存。v1と同形式）
- 声・速度などの選択肢、権限チェック（管理者 / 信頼ユーザー）
- libopus ローダ: BOTフォルダの libopus.so.0 があればそれを使う
  （2026-11-13 の診断で、libopus 未ロードだと再生は動くのに無音になる
  ことが分かったため、v1 から移植）
"""

import json
import os
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

TOKEN = os.getenv("DISCORD_TOKEN")
CONFIG_FILE = BASE_DIR / "tts_config.json"
TMP_DIR = BASE_DIR / "tmp_tts"

# 管理者権限チェックをバイパスできるユーザーID（他BOTと共通の運用）
TRUSTED_USER_IDS = {1546348374922625065, 1256574550901133377}

# Edge TTS の日本語ボイス（無料・キー不要）
VOICES = {
    "nanami": ("ja-JP-NanamiNeural", "Nanami（女性）"),
    "keita": ("ja-JP-KeitaNeural", "Keita（男性）"),
}
DEFAULT_VOICE = "nanami"

# 読み上げ速度の候補（edge-tts の rate 表記）
RATES = ["-20%", "+0%", "+25%", "+50%"]
RATE_LABELS = {"-20%": "ゆっくり", "+0%": "普通", "+25%": "やや速い", "+50%": "速い"}

# 1メッセージの最大読み上げ文字数の候補
MAX_CHARS_CHOICES = [50, 100, 200, 300]

ALONE_LEAVE_SEC = 15     # VCが空になってから退出するまでの猶予（秒）
QUEUE_MAX = 20           # 読み上げキューの上限（超過分は捨てる）

DEFAULT_CONFIG = {
    "read_channel_id": None,     # 読み上げるテキストチャンネル
    "target_vc_id": None,        # None = 自動（人がいるVC） / ID = そのVC固定
    "enabled": True,             # 読み上げ ON/OFF
    "read_name": True,           # 発言者名を読む
    "greetings": True,           # 入退室あいさつ
    "only_vc_members": False,    # True = VC参加中の人の発言だけ読む
    "read_vc_chat": True,        # BOTが接続中のVC内チャットも読み上げる
    "default_voice": DEFAULT_VOICE,
    "rate": "+0%",
    "max_chars": 100,
    "dictionary": {},            # サーバー共通の単語辞書 {単語: 読み}
    "users": {},                 # {uid: {"voice": key, "muted": bool}}
    "panel": None,               # {"channel": id, "message": id}
}


class Store:
    """サーバーごとの設定を tts_config.json に保存する。"""

    def __init__(self):
        self.data: dict[str, dict] = {}
        try:
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.data = raw
        except Exception:
            self.data = {}

    def cfg(self, guild_id: int) -> dict:
        key = str(guild_id)
        cfg = self.data.get(key)
        if cfg is None:
            cfg = dict(DEFAULT_CONFIG)
            cfg["dictionary"] = {}
            cfg["users"] = {}
            self.data[key] = cfg
        else:
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
        return cfg

    def save(self):
        try:
            CONFIG_FILE.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except Exception as e:
            print(f"⚠️ 設定の保存に失敗: {e}")


# ---------------------------------------------------------------- libopus（音声エンコード用）
# これが読み込めない環境だと、再生処理は動くのに音声フレームが作られず
# 「誰にも聞こえない」状態になる。BOTフォルダに libopus.so.0 を置けばそれを使う。
# v1 から移植（2026-11-13）。
def _load_opus() -> str:
    import ctypes.util
    import discord.opus
    if discord.opus.is_loaded():
        return "already"
    candidates = [str(BASE_DIR / "libopus.so.0"), "libopus.so.0", "libopus.so",
                  ctypes.util.find_library("opus")]
    for cand in candidates:
        if not cand:
            continue
        try:
            discord.opus.load_opus(cand)
            return cand
        except Exception:
            continue
    return ""

OPUS_SOURCE = _load_opus()
if OPUS_SOURCE:
    print(f"🎧 libopus: OK ({OPUS_SOURCE})")
else:
    print("⚠️ libopus を読み込めませんでした — 音声が送られない可能性があります")

store = Store()


def is_privileged(member) -> bool:
    """管理者 または 信頼ユーザー か。"""
    if member is None:
        return False
    if getattr(member, "id", None) in TRUSTED_USER_IDS:
        return True
    perms = getattr(member, "guild_permissions", None)
    return bool(perms and perms.administrator)


def user_setting(cfg: dict, user_id: int) -> dict:
    return cfg["users"].setdefault(str(user_id), {"voice": None, "muted": False})


def voice_of(cfg: dict, user_id: int) -> str:
    """その人の声キー（未設定ならサーバーのデフォルト声）。"""
    key = user_setting(cfg, user_id).get("voice")
    return key if key in VOICES else cfg.get("default_voice", DEFAULT_VOICE)


class TtsBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.voice_states = True
        intents.message_content = True   # 発言本文を読むために必須
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # 永続パネルの登録は on_ready でサーバーごとに行う（panel.py）。
        # ここではコマンド同期だけする。
        await self.tree.sync()

    async def on_ready(self):
        print(f"✅ ログインしました: {self.user} (ID: {self.user.id})")
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.listening, name="読み上げ待機中")
        )


bot = TtsBot()
