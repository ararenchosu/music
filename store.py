# -*- coding: utf-8 -*-
"""
store.py — 永続化（data.json）

保存するもの:
- guilds:    サーバーごとの設定（DJロール / デフォルト音量 / 自動退出秒数 /
             常駐モード / 再生開始のお知らせ）
- panels:    常設パネルの設置場所（guild_id -> {channel, message}）
- favorites: ユーザーごとのお気に入り曲リスト
- mylists:   ユーザーごとのマイリスト（名前 -> 曲リスト）
- history:   サーバーごとの再生履歴（直近分のみ）

書き込みは「全部を1回で書く」単純な方式。スマホ運用の小規模BOT前提で、
競合よりも壊れにくさを優先する（書き込み途中の破損に備えて一時ファイル経由）。
"""

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data.json"

DEFAULT_GUILD_CONFIG = {
    "dj_role_id": None,        # DJロールID（未設定なら全員操作可）
    "default_volume": 100,     # 0-200
    "idle_seconds": 180,       # 無再生が続いたら退出するまでの秒数
    "alone_seconds": 60,       # BOTだけがVCに残ったら退出するまでの秒数
    "stay_247": False,         # 24時間常駐モード（自動退出を無効化）
    "announce": True,          # 再生開始通知を出すか
    "ducking": True,           # 読み上げBOTの発話中は音楽を自動で小さくする
    "announce_tts": True,      # 曲の開始を読み上げBOTに読み上げてもらう
    "repost_panel": True,      # 再生開始通知の直後にパネルを一番下へ再設置するか
    "autoplay": False,         # キューが尽きたら関連曲を自動で流し続けるか
    "last_vc_id": None,        # 最後に接続していたVC（常駐時の自動復帰先）
    "silent_notify": False,    # BOTのお知らせを通知なし（サイレント）で送るか
    "autoplay_seed": "",       # 旧・単一種曲（互換用。通常は autoplay_seeds を使う）
    "autoplay_seeds": [],      # オートプレイの種曲リスト（複数可）
}

HISTORY_LIMIT = 50

_data: dict = {
    "guilds": {},
    "panels": {},        # メインパネル
    "sub_panels": {},    # サブパネル（再生中の曲情報）
    "lyrics_panels": {},  # 歌詞パネル
    "favorites": {},
    "mylists": {},
    "history": {},
}

PANEL_KINDS = ("panels", "sub_panels", "lyrics_panels")


def load_data() -> dict:
    """data.json を読む。無ければデフォルト構造で開始する。"""
    global _data
    try:
        raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for key in _data:
                if isinstance(raw.get(key), dict):
                    _data[key] = raw[key]
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"⚠️ data.json の読み込みに失敗（初期状態で開始）: {e}")
    return _data


def save_data() -> None:
    """一時ファイルへ書いてから置き換える（途中で落ちても本体を壊さない）。"""
    try:
        tmp = DATA_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, DATA_FILE)
    except Exception as e:
        print(f"⚠️ data.json の保存に失敗: {e}")


def data() -> dict:
    return _data


# ---------------------------------------------------------------- サーバー設定
def guild_config(guild_id: int) -> dict:
    """サーバー設定を返す（無ければデフォルトで作る）。"""
    key = str(guild_id)
    cfg = _data["guilds"].get(key)
    if cfg is None:
        cfg = dict(DEFAULT_GUILD_CONFIG)
        _data["guilds"][key] = cfg
        save_data()
    else:
        # 旧データに項目が足りない場合はデフォルトで補う
        for k, v in DEFAULT_GUILD_CONFIG.items():
            cfg.setdefault(k, v)
    # リスト型デフォルトの共有を防ぐ（サーバー間で同じリストを掴まない）
    if cfg.get("autoplay_seeds") is DEFAULT_GUILD_CONFIG["autoplay_seeds"]:
        cfg["autoplay_seeds"] = []
    return cfg


def set_guild_config(guild_id: int, **kwargs) -> dict:
    cfg = guild_config(guild_id)
    cfg.update(kwargs)
    save_data()
    return cfg


# ---------------------------------------------------------------- パネル設置場所
def save_panel_loc(kind: str, guild_id: int, channel_id: int, message_id: int) -> None:
    """kind: panels / sub_panels / lyrics_panels"""
    _data[kind][str(guild_id)] = {"channel": channel_id, "message": message_id}
    save_data()


def panel_loc(kind: str, guild_id: int) -> dict | None:
    return _data[kind].get(str(guild_id))


def all_panel_locs(kind: str) -> dict:
    return dict(_data[kind])


def drop_panel_loc(kind: str, guild_id: int) -> None:
    _data[kind].pop(str(guild_id), None)
    save_data()


def save_panel(guild_id: int, channel_id: int, message_id: int) -> None:
    save_panel_loc("panels", guild_id, channel_id, message_id)


def panel_locations() -> dict:
    return all_panel_locs("panels")


def drop_panel(guild_id: int) -> None:
    drop_panel_loc("panels", guild_id)


# ---------------------------------------------------------------- お気に入り
def favorites(user_id: int) -> list:
    return _data["favorites"].setdefault(str(user_id), [])


def add_favorite(user_id: int, track_dict: dict) -> bool:
    """追加したら True、既に同じURLがあれば False。"""
    favs = favorites(user_id)
    if any(f.get("webpage_url") == track_dict.get("webpage_url") for f in favs):
        return False
    favs.append(track_dict)
    save_data()
    return True


def remove_favorite(user_id: int, index: int) -> dict | None:
    favs = favorites(user_id)
    if 0 <= index < len(favs):
        removed = favs.pop(index)
        save_data()
        return removed
    return None


# ---------------------------------------------------------------- マイリスト
def mylists(user_id: int) -> dict:
    return _data["mylists"].setdefault(str(user_id), {})


def create_mylist(user_id: int, name: str) -> bool:
    lists = mylists(user_id)
    if name in lists:
        return False
    lists[name] = []
    save_data()
    return True


def delete_mylist(user_id: int, name: str) -> bool:
    lists = mylists(user_id)
    if name not in lists:
        return False
    del lists[name]
    save_data()
    return True


def add_to_mylist(user_id: int, name: str, track_dict: dict) -> bool:
    lists = mylists(user_id)
    if name not in lists:
        return False
    lists[name].append(track_dict)
    save_data()
    return True


def remove_from_mylist(user_id: int, name: str, index: int) -> dict | None:
    lists = mylists(user_id)
    tracks = lists.get(name)
    if tracks is None or not (0 <= index < len(tracks)):
        return None
    removed = tracks.pop(index)
    save_data()
    return removed


# ---------------------------------------------------------------- 再生履歴
def push_history(guild_id: int, track_dict: dict) -> None:
    hist = _data["history"].setdefault(str(guild_id), [])
    hist.insert(0, track_dict)
    del hist[HISTORY_LIMIT:]
    save_data()


def history(guild_id: int, limit: int = 10) -> list:
    return list(_data["history"].get(str(guild_id), []))[:limit]
