# -*- coding: utf-8 -*-
"""
start.py — 音楽BOT + 読み上げBOT の同時起動ランチャー（リポジトリ構成版）

構成:
    music_bot.py ほか   … 音楽BOT（このフォルダ直下、今まで通り）
    tts/tts_bot.py      … 読み上げBOT
    .env                … トークン2つ:
                            MUSIC_DISCORD_TOKEN=音楽BOT
                            DISCORD_TOKEN=読み上げBOT

- python start.py で2つとも起動する。
- 片方が落ちたら5秒後に自動で再起動する（常駐用）。
- 止める時は Ctrl+C（両方止まる）。
- 片方のトークンが未設定なら、そのBOTはスキップして片方だけ起動する。
"""

import os
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent

# ルートの .env を読み込んで子プロセスへ引き継ぐ
try:
    from dotenv import load_dotenv
    load_dotenv(BASE / ".env")
except Exception:
    env_file = BASE / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

BOTS = [
    ("🎵 音楽BOT", BASE / "music_bot.py", BASE, "MUSIC_DISCORD_TOKEN"),
    ("🗣 読み上げBOT", BASE / "tts" / "tts_bot.py", BASE / "tts", "DISCORD_TOKEN"),
]


def spawn(name: str, script: Path, cwd: Path) -> subprocess.Popen:
    print(f"{name} を起動します: {script}", flush=True)
    return subprocess.Popen(
        [sys.executable, str(script)],
        cwd=str(cwd),
        env=os.environ.copy(),
    )


def main():
    procs: dict[str, subprocess.Popen] = {}
    meta: dict[str, tuple[Path, Path]] = {}
    for name, script, cwd, token_key in BOTS:
        if not script.exists():
            print(f"⚠️ {script} が無いので {name} は起動しません", flush=True)
            continue
        if not os.getenv(token_key):
            print(f"⚠️ {token_key} が未設定なので {name} は起動しません（.env を確認）", flush=True)
            continue
        procs[name] = spawn(name, script, cwd)
        meta[name] = (script, cwd)
    if not procs:
        print("❌ 起動できるBOTがありません。.env とファイル配置を確認してください")
        return
    print("✅ 起動完了。止める時は Ctrl+C", flush=True)
    try:
        while True:
            time.sleep(2)
            for name, p in list(procs.items()):
                if p.poll() is not None:
                    script, cwd = meta[name]
                    print(f"⚠️ {name} が終了しました（code={p.returncode}）。5秒後に再起動します", flush=True)
                    time.sleep(5)
                    procs[name] = spawn(name, script, cwd)
    except KeyboardInterrupt:
        print("\n🛑 両方のBOTを止めます…", flush=True)
        for p in procs.values():
            if p.poll() is None:
                p.terminate()
        for p in procs.values():
            try:
                p.wait(timeout=10)
            except Exception:
                p.kill()
        print("👋 終了しました", flush=True)


if __name__ == "__main__":
    main()
