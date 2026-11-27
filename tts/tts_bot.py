# -*- coding: utf-8 -*-
"""
tts_bot.py — VC常駐読み上げBOT v2（作り直し版）
================================================
誰かがボイスチャンネルにいる間だけ常駐し、指定テキストチャンネルの発言と
BOTがいるVC内のチャットを Edge TTS（無料・キー不要）で読み上げる。

  tts_bot.py    起動ファイル（ホストの起動ファイルはこれ）
  core.py       設定・BOT本体
  speech.py     読み上げエンジン（整形・合成・再生キュー）
  resident.py   VC常駐ロジック（自動入室/退出・あいさつ）
  panel.py      設定パネル（全部セレクトメニュー式・起動時自動更新）
  commands.py   スラッシュコマンド（診断 /voicediag・/ttstest 付き）

v1からの直し（作り直しの理由）:
  1. パネルの全セレクトが応答前に defer する — 重い処理でも
     Discordの3秒期限切れ (Unknown interaction 10062) にならない。
  2. 永続パネルをサーバーごとに登録する — 再起動後もそのサーバーの
     VC一覧つきパネルが生きる（v1は guild=None の1枚だけだった）。
  3. 再生待ちをポーリングから afterコールバック+Event に変更。
  4. 診断コマンドを最初から内蔵（/voicediag・/ttstest）。

必要な環境変数: DISCORD_TOKEN（.env に1行）
開発者ポータルで MESSAGE CONTENT INTENT を ON にすること。
設定ファイル tts_config.json は v1 と同じ形式なので、同じフォルダに
置き換えれば設定・パネル設置場所は引き継がれる。
"""

from core import bot, TOKEN

import speech    # noqa: F401  発言リスナー登録
import resident  # noqa: F401  常駐イベント登録
import panel     # noqa: F401  パネル自動更新リスナー登録
import commands  # noqa: F401  スラッシュコマンド登録

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("❌ DISCORD_TOKEN が設定されていません")
    bot.run(TOKEN)
