# -*- coding: utf-8 -*-
"""
music_bot.py — 起動のみ

トークンは環境変数 MUSIC_DISCORD_TOKEN（または同フォルダの .env）から読む。
import しただけでは接続も sync も走らない（検証用に import 可能）。
"""

from core import bot, TOKEN

import commands  # noqa: F401  コマンド登録のため import する
import panel  # noqa: F401  パネルView定義の読み込み

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("❌ MUSIC_DISCORD_TOKEN が設定されていません（.env を確認してください）")
    bot.run(TOKEN)
