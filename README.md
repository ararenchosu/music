# 超高機能音楽BOT（新規設計）

既存の音楽BOT v2 とは別物の、セレクトメニュー式UIを持つ音楽BOTです。

## セットアップ（スマホ運用前提）

1. このフォルダ一式をホストへアップロードする
2. `.env.example` を `.env` に変え、`MUSIC_DISCORD_TOKEN` に新しいBOTのトークンを入れる
3. `pip install -r requirements.txt`
4. 起動ファイルとして `music_bot.py` を指定して起動する
5. サーバーで `/パネル設置` を実行すると常設パネルが出る（再起動しても自動で復活する）

## 使い方の要点

- `/再生 <曲名 or URL>` — YouTube / SoundCloud / Spotify 対応
  - Spotifyは埋め込みページから曲名を取り、YouTube→SoundCloudの順で探します（APIキー不要）
  - **YouTubeが取得できないホストでは、検索が自動で SoundCloud にフォールバックします**
- パネルはセレクト1つで操作（曲追加はモーダルが開きます）
- `/設定`（管理者）— DJロール、デフォルト音量、自動退出秒数、24時間常駐、お知らせON/OFF
- `/診断`（管理者）— YouTube側のIPブロック判定と SoundCloud 側の疎通を両方確認できます

## データ

- `data.json` にサーバー設定・パネル設置場所・お気に入り・マイリスト・再生履歴が保存されます
- YouTube用クッキーが必要なホストでは、同フォルダに `cookies.txt` を置くか `.env` の `YT_COOKIES_FILE` / `YT_COOKIES_B64` を使います

## 注意

- フィルターは次曲から適用されます（再生中の曲には遡りません）
- 無料ホストではホストのIPがYouTubeにブロックされることがあります。その場合でも SoundCloud 音源は再生できます。状態は `/診断` で確認してください
