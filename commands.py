# -*- coding: utf-8 -*-
"""
commands.py — 日本語スラッシュコマンド（全21個）

再生系:      /再生 /一時停止 /再開 /スキップ /停止 /キュー /キュー削除
             /全消去 /ループ /シャッフル /音量 /フィルター /履歴
個人機能:    /お気に入り /マイリスト
運営系:      /接続 /パネル設置 /設定 /診断 /音声診断 /ヘルプ
"""

import time

import discord
from discord import app_commands
from discord.ext import commands as ext_commands

import store
from core import bot, Track, COOKIE_FILE, filter_label, FILTERS, FFMPEG, OPUS_LOADED
from player import (get_player, ensure_voice, add_query, add_tracks,
                    disconnect_player, set_volume, cycle_loop, LOOP_TEXT,
                    can_control, _wait_encrypt_ready, _can_encrypt)
from panel import (MusicPanelView, panel_embed, queue_embed,
                   FilterView, VolumeView, QueueRemoveView, SettingsView,
                   TrackPickView, NamePickView)
from sources import (extract_raw_youtube, summarize_formats,
                     check_soundcloud, BUILD_TAG)


def _player(interaction: discord.Interaction):
    return get_player(interaction.guild_id)


async def _require_control(interaction: discord.Interaction) -> bool:
    if can_control(interaction):
        return True
    await interaction.response.send_message(
        "この操作はDJロール（または管理者）が必要です", ephemeral=True)
    return False


# ---------------------------------------------------------------- 再生
@bot.tree.command(name="再生", description="曲を再生 / キューに追加します")
@app_commands.describe(入力="曲名 / YouTube・SoundCloud URL / Spotifyリンク")
async def cmd_play(interaction: discord.Interaction, 入力: str):
    if not await _require_control(interaction):
        return
    await interaction.response.defer()
    player = _player(interaction)
    try:
        await ensure_voice(player, interaction.user)
        added, note = await add_query(bot, player, 入力, interaction.user.display_name,
                                      text_channel_id=interaction.channel_id)
    except Exception as e:
        await interaction.followup.send(f"❌ {e}")
        return
    if len(added) == 1:
        msg = f"🎵 **{added[0].title}** を追加しました"
    else:
        msg = f"🎵 {len(added)}曲を追加しました"
    if note:
        msg += f"\n{note}"
    await interaction.followup.send(msg)


@bot.tree.command(name="一時停止", description="再生を一時停止します")
async def cmd_pause(interaction: discord.Interaction):
    if not await _require_control(interaction):
        return
    player = _player(interaction)
    if player.voice and player.voice.is_playing():
        player.voice.pause()
        await interaction.response.send_message("⏸ 一時停止しました", ephemeral=True)
    else:
        await interaction.response.send_message("再生中の曲がありません", ephemeral=True)


@bot.tree.command(name="再開", description="一時停止中の曲を再開します")
async def cmd_resume(interaction: discord.Interaction):
    if not await _require_control(interaction):
        return
    player = _player(interaction)
    if player.voice and player.voice.is_paused():
        player.voice.resume()
        await interaction.response.send_message("▶️ 再開しました", ephemeral=True)
    else:
        await interaction.response.send_message("一時停止中の曲がありません", ephemeral=True)


@bot.tree.command(name="スキップ", description="再生中の曲をスキップします")
async def cmd_skip(interaction: discord.Interaction):
    if not await _require_control(interaction):
        return
    player = _player(interaction)
    if player.voice and (player.voice.is_playing() or player.voice.is_paused()):
        player.voice.stop()
        await interaction.response.send_message("⏭ スキップしました", ephemeral=True)
    else:
        await interaction.response.send_message("再生中の曲がありません", ephemeral=True)


@bot.tree.command(name="停止", description="再生を止めてキューを空にし、VCから退出します")
async def cmd_stop(interaction: discord.Interaction):
    if not await _require_control(interaction):
        return
    await disconnect_player(bot, _player(interaction))
    await interaction.response.send_message("⏹ 停止しました", ephemeral=True)


@bot.tree.command(name="キュー", description="再生中とキューの中身を表示します")
async def cmd_queue(interaction: discord.Interaction):
    await interaction.response.send_message(embed=queue_embed(_player(interaction)),
                                            ephemeral=True)


@bot.tree.command(name="キュー削除", description="キューから曲を選んで削除します")
async def cmd_queue_remove(interaction: discord.Interaction):
    if not await _require_control(interaction):
        return
    player = _player(interaction)
    if not player.queue:
        await interaction.response.send_message("キューが空です", ephemeral=True)
        return
    await interaction.response.send_message(
        "削除する曲を選んでください", view=QueueRemoveView(bot, player), ephemeral=True)


@bot.tree.command(name="全消去", description="キューの待機曲を全て消します（再生中は止めません）")
async def cmd_clear(interaction: discord.Interaction):
    if not await _require_control(interaction):
        return
    n = _player(interaction).clear_queue()
    await interaction.response.send_message(f"🗑 キューから {n} 曲を消しました", ephemeral=True)


@bot.tree.command(name="ループ", description="ループ設定を切り替えます（オフ→曲→キュー全体）")
async def cmd_loop(interaction: discord.Interaction):
    if not await _require_control(interaction):
        return
    player = _player(interaction)
    mode = cycle_loop(player)
    await interaction.response.send_message(f"🔁 ループ設定: {LOOP_TEXT[mode]}", ephemeral=True)


@bot.tree.command(name="シャッフル", description="キューの待機曲をシャッフルします")
async def cmd_shuffle(interaction: discord.Interaction):
    if not await _require_control(interaction):
        return
    player = _player(interaction)
    if not player.queue:
        await interaction.response.send_message("キューが空です", ephemeral=True)
        return
    player.shuffle_queue()
    await interaction.response.send_message("🔀 キューをシャッフルしました", ephemeral=True)


@bot.tree.command(name="音量", description="音量を設定します（0-200）。数値なしで選択メニューを出します")
@app_commands.describe(数値="0から200までの数値（省略可）")
async def cmd_volume(interaction: discord.Interaction, 数値: int = -1):
    if not await _require_control(interaction):
        return
    player = _player(interaction)
    if 数値 < 0:
        await interaction.response.send_message(
            "音量を選んでください", view=VolumeView(bot), ephemeral=True)
        return
    applied = set_volume(player, 数値)
    await interaction.response.send_message(f"🔊 音量を {applied}% にしました", ephemeral=True)


@bot.tree.command(name="フィルター", description="音声フィルターを選びます（次曲から適用）")
async def cmd_filter(interaction: discord.Interaction):
    if not await _require_control(interaction):
        return
    await interaction.response.send_message(
        f"現在のフィルター: **{filter_label(_player(interaction).filter_key)}**\n変更する場合は選んでください（次曲から適用）",
        view=FilterView(bot), ephemeral=True)


@bot.tree.command(name="履歴", description="このサーバーで最近再生した曲を表示します")
async def cmd_history(interaction: discord.Interaction):
    hist = store.history(interaction.guild_id, limit=10)
    if not hist:
        await interaction.response.send_message("まだ再生履歴がありません", ephemeral=True)
        return
    lines = [f"{i + 1}. {h.get('title', '（タイトル不明）')}" for i, h in enumerate(hist)]
    embed = discord.Embed(title="🕘 再生履歴（直近10件）", color=0x5865F2,
                          description="\n".join(lines))
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------- お気に入り
@bot.tree.command(name="お気に入り", description="お気に入りの追加 / 一覧 / 再生 / 削除")
@app_commands.describe(操作="やりたいこと")
@app_commands.choices(操作=[
    app_commands.Choice(name="再生中の曲を追加", value="add_current"),
    app_commands.Choice(name="一覧", value="list"),
    app_commands.Choice(name="再生（選んで追加）", value="play"),
    app_commands.Choice(name="削除（選んで削除）", value="remove"),
])
async def cmd_favorite(interaction: discord.Interaction, 操作: str):
    uid = interaction.user.id
    player = _player(interaction)
    if 操作 == "add_current":
        if not player.current:
            await interaction.response.send_message("再生中の曲がありません", ephemeral=True)
            return
        added = store.add_favorite(uid, player.current.to_dict())
        await interaction.response.send_message(
            "⭐ お気に入りに追加しました" if added else "既にお気に入りに入っています",
            ephemeral=True)
        return
    favs = store.favorites(uid)
    if not favs:
        await interaction.response.send_message("お気に入りはまだ空です", ephemeral=True)
        return
    if 操作 == "list":
        lines = [f"{i + 1}. {f.get('title', '（タイトル不明）')}" for i, f in enumerate(favs[:25])]
        embed = discord.Embed(title="⭐ お気に入り", color=0xF1C40F,
                              description="\n".join(lines))
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    if 操作 == "play":
        async def handler(inter: discord.Interaction, index: int):
            fav = store.favorites(inter.user.id)[index]
            track = Track.from_dict(fav, inter.user.display_name)
            try:
                await ensure_voice(player, inter.user)
                await add_tracks(bot, player, [track], text_channel_id=inter.channel_id)
                await inter.followup.send(f"⭐ **{track.title}** を追加しました", ephemeral=True)
            except Exception as e:
                await inter.followup.send(f"❌ {e}", ephemeral=True)
        await interaction.response.send_message(
            "再生する曲を選んでください",
            view=TrackPickView(bot, favs, handler, "お気に入りから選んでください"),
            ephemeral=True)
        return
    if 操作 == "remove":
        async def handler(inter: discord.Interaction, index: int):
            removed = store.remove_favorite(inter.user.id, index)
            await inter.followup.send(
                f"🗑 **{removed['title']}** をお気に入りから削除しました" if removed
                else "その曲は既にありません", ephemeral=True)
        await interaction.response.send_message(
            "削除する曲を選んでください",
            view=TrackPickView(bot, favs, handler, "削除する曲を選んでください"),
            ephemeral=True)
        return


# ---------------------------------------------------------------- マイリスト
@bot.tree.command(name="マイリスト", description="自分専用プレイリストの作成 / 追加 / 再生 / 削除")
@app_commands.describe(操作="やりたいこと", 名前="リスト名（作成・追加・再生・削除で使用）")
@app_commands.choices(操作=[
    app_commands.Choice(name="リストを作成", value="create"),
    app_commands.Choice(name="再生中の曲をリストに追加", value="add_current"),
    app_commands.Choice(name="一覧", value="list"),
    app_commands.Choice(name="再生（リストを全曲追加）", value="play"),
    app_commands.Choice(name="リストを削除", value="delete"),
    app_commands.Choice(name="リストから曲を削除", value="remove_track"),
])
async def cmd_mylist(interaction: discord.Interaction, 操作: str, 名前: str = ""):
    uid = interaction.user.id
    player = _player(interaction)
    lists = store.mylists(uid)
    名前 = 名前.strip()

    if 操作 == "create":
        if not 名前:
            await interaction.response.send_message("リスト名を入力してください", ephemeral=True)
            return
        ok = store.create_mylist(uid, 名前)
        await interaction.response.send_message(
            f"📂 マイリスト「{名前}」を作成しました" if ok else "その名前のリストは既にあります",
            ephemeral=True)
        return
    if 操作 == "list":
        if not lists:
            await interaction.response.send_message("マイリストはまだありません", ephemeral=True)
            return
        lines = [f"📂 **{name}**（{len(tracks)}曲）" for name, tracks in lists.items()]
        embed = discord.Embed(title="📂 マイリスト一覧", color=0x2ECC71,
                              description="\n".join(lines))
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    if 操作 == "add_current":
        if not player.current:
            await interaction.response.send_message("再生中の曲がありません", ephemeral=True)
            return
        if 名前:
            if store.add_to_mylist(uid, 名前, player.current.to_dict()):
                await interaction.response.send_message(
                    f"📂 「{名前}」に **{player.current.title}** を追加しました", ephemeral=True)
            else:
                await interaction.response.send_message("そのリストがありません", ephemeral=True)
            return
        if not lists:
            await interaction.response.send_message("マイリストはまだありません", ephemeral=True)
            return

        async def handler(inter: discord.Interaction, name: str):
            p = get_player(inter.guild_id)
            if p.current and store.add_to_mylist(inter.user.id, name, p.current.to_dict()):
                await inter.followup.send(f"📂 「{name}」に追加しました", ephemeral=True)
            else:
                await inter.followup.send("追加できませんでした", ephemeral=True)
        await interaction.response.send_message(
            "追加先のリストを選んでください",
            view=NamePickView(bot, list(lists.keys()), handler, "追加先のリスト"),
            ephemeral=True)
        return
    # 以下は対象リストの指定が必要
    if not lists:
        await interaction.response.send_message("マイリストはまだありません", ephemeral=True)
        return

    async def play_handler(inter: discord.Interaction, name: str):
        tracks = [Track.from_dict(t, inter.user.display_name)
                  for t in store.mylists(inter.user.id).get(name, [])]
        if not tracks:
            await inter.followup.send("そのリストは空です", ephemeral=True)
            return
        try:
            await ensure_voice(player, inter.user)
            await add_tracks(bot, player, tracks, text_channel_id=inter.channel_id)
            await inter.followup.send(f"📂 「{name}」から {len(tracks)} 曲を追加しました",
                                      ephemeral=True)
        except Exception as e:
            await inter.followup.send(f"❌ {e}", ephemeral=True)

    async def delete_handler(inter: discord.Interaction, name: str):
        store.delete_mylist(inter.user.id, name)
        await inter.followup.send(f"🗑 マイリスト「{name}」を削除しました", ephemeral=True)

    if 操作 == "play":
        if 名前 and 名前 in lists:
            await interaction.response.defer(ephemeral=True)
            await play_handler(interaction, 名前)
            return
        await interaction.response.send_message(
            "再生するリストを選んでください",
            view=NamePickView(bot, list(lists.keys()), play_handler, "再生するリスト"),
            ephemeral=True)
        return
    if 操作 == "delete":
        if 名前 and 名前 in lists:
            store.delete_mylist(uid, 名前)
            await interaction.response.send_message(
                f"🗑 マイリスト「{名前}」を削除しました", ephemeral=True)
            return
        await interaction.response.send_message(
            "削除するリストを選んでください",
            view=NamePickView(bot, list(lists.keys()), delete_handler, "削除するリスト"),
            ephemeral=True)
        return
    if 操作 == "remove_track":
        target = 名前 if 名前 in lists else None
        if target is None:
            await interaction.response.send_message(
                "まず /マイリスト で「リストから曲を削除」を選び、名前に対象リスト名を入れてください",
                ephemeral=True)
            return
        tracks = lists[target]
        if not tracks:
            await interaction.response.send_message("そのリストは空です", ephemeral=True)
            return

        async def remove_handler(inter: discord.Interaction, index: int, _name=target):
            removed = store.remove_from_mylist(inter.user.id, _name, index)
            await inter.followup.send(
                f"🗑 **{removed['title']}** を削除しました" if removed else "その曲は既にありません",
                ephemeral=True)
        await interaction.response.send_message(
            "削除する曲を選んでください",
            view=TrackPickView(bot, tracks, remove_handler, "削除する曲を選んでください"),
            ephemeral=True)
        return


# ---------------------------------------------------------------- 運営系
class VoiceChannelPickView(discord.ui.View):
    """BOTだけを先にVCへ入れるためのチャンネル選択（/接続 用・一時的View）。"""

    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=60)
        options = [discord.SelectOption(label=ch.name[:100], value=str(ch.id))
                   for ch in guild.voice_channels[:25]]
        select = discord.ui.Select(
            placeholder="BOTを入れるボイスチャンネルを選んでください",
            options=options, custom_id="super_music:vc_pick")
        select.callback = self._on_pick
        self.add_item(select)

    async def _on_pick(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        select = self.children[0]
        channel_id = int(select.values[0])
        channel = interaction.guild.get_channel(channel_id)
        player = get_player(interaction.guild_id)
        try:
            if player.voice and player.voice.is_connected():
                if player.voice.channel.id != channel_id:
                    await player.voice.move_to(channel)
            else:
                player.voice = await channel.connect(self_deaf=True)
            ok = await _wait_encrypt_ready(player.voice)
            text = (f"🔊 **{channel.name}** にBOTが入りました。"
                    f"暗号化準備: {'✅ OK' if ok else '⏱ タイムアウト（まだ暗号化可になっていません）'}\n"
                    "次に、あなたが同じVCへ入ってください。その後 /再生 で曲をかけてください。")
        except Exception as e:
            text = f"❌ 接続に失敗しました: {e}"
        select.disabled = True
        try:
            await interaction.edit_original_response(content=text, view=self)
        except discord.NotFound:
            pass


@bot.tree.command(name="接続", description="BOTだけを先にボイスチャンネルへ入れます（暗号化の順番テスト用）")
async def cmd_connect_first(interaction: discord.Interaction):
    if not await _require_control(interaction):
        return
    if not interaction.guild.voice_channels:
        await interaction.response.send_message("ボイスチャンネルがありません", ephemeral=True)
        return
    await interaction.response.send_message(
        "BOTを先に入れるチャンネルを選んでください（あなたはまだ入らないでください）",
        view=VoiceChannelPickView(interaction.guild), ephemeral=True)


@bot.tree.command(name="パネル設置", description="常設の音楽パネルをこのチャンネルに設置します")
@app_commands.default_permissions(administrator=True)
async def cmd_panel_setup(interaction: discord.Interaction):
    await interaction.response.send_message(embed=panel_embed(_player(interaction)),
                                            view=MusicPanelView(bot))
    message = await interaction.original_response()
    store.save_panel(interaction.guild_id, interaction.channel_id, message.id)


@bot.tree.command(name="設定", description="サーバー設定（DJロール / 音量 / 自動退出 / 常駐 / お知らせ）")
@app_commands.default_permissions(administrator=True)
async def cmd_settings(interaction: discord.Interaction):
    cfg = store.guild_config(interaction.guild_id)
    lines = [
        f"・DJロール: {cfg.get('dj_role_id') or '未設定（全員操作可）'}",
        f"・デフォルト音量: {cfg.get('default_volume')}%",
        f"・無再生時の自動退出: {cfg.get('idle_seconds')}秒",
        f"・BOTだけ残った時の自動退出: {cfg.get('alone_seconds')}秒",
        f"・24時間常駐モード: {'ON' if cfg.get('stay_247') else 'OFF'}",
        f"・再生開始のお知らせ: {'ON' if cfg.get('announce') else 'OFF'}",
        f"・お知らせ後にパネルを再設置: {'ON' if cfg.get('repost_panel') else 'OFF'}",
    ]
    await interaction.response.send_message(
        "⚙️ **現在の設定**\n" + "\n".join(lines) + "\n\n変更する項目を選んでください",
        view=SettingsView(bot, interaction.guild_id), ephemeral=True)


@bot.tree.command(name="診断", description="YouTube / SoundCloud の取得状態を診断します（管理者専用）")
@app_commands.default_permissions(administrator=True)
async def cmd_diagnose(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    lines = ["🧪 診断結果", f"- ビルド: {BUILD_TAG}",
             f"- クッキー: {'使用中' if COOKIE_FILE else 'なし'}"]
    started = time.time()
    raw_audio = None
    try:
        raw = await extract_raw_youtube("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        s = summarize_formats(raw.get("formats") or [])
        raw_audio = s["playable"]
        lines.append(f"- YouTube 生フォーマット総数: {s['total']}")
        lines.append(f"- YouTube うち音声: {s['playable']}（音声のみ {s['audio_only']}）")
        lines.append(f"- YouTube storyboard: {s['storyboards']}")
    except Exception as e:
        text = str(e)
        if "Sign in to confirm" in text:
            lines.append("- YouTube 生取得: ❌ bot判定（認証が通っていない）")
        else:
            lines.append(f"- YouTube 生取得エラー: {text[:200]}")
    if raw_audio:
        lines.append("- YouTube判定: ✅ 音声が返っています")
    elif raw_audio == 0:
        lines.append("- YouTube判定: ❌ 音声が1つも渡されていません（IPブロックの症状）。"
                       "この場合も検索は SoundCloud へ自動フォールバックします")
    else:
        lines.append("- YouTube判定: ❌ 生情報自体が取得できませんでした")
    ok, detail = await check_soundcloud()
    lines.append(f"- SoundCloud判定: {'✅' if ok else '❌'} {detail}")
    lines.append(f"- 取得時間: {time.time() - started:.1f}秒")
    await interaction.followup.send("\n".join(lines), ephemeral=True)


@bot.tree.command(name="音声診断", description="音が出ない時に、音声まわり（libopus / DAVE暗号化）とテスト音を確認します")
async def cmd_voice_diag(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    import importlib.metadata as md

    def ver(pkg):
        try:
            return md.version(pkg)
        except Exception:
            return "未インストール"

    lines = [
        "**🔍 音声診断**",
        f"discord.py: {ver('discord.py')}",
        f"davey（音声の暗号化に必要）: {ver('davey')}",
        f"PyNaCl: {ver('PyNaCl')}",
        f"libopus（音声エンコード）: {'ロード済み' if OPUS_LOADED else '未ロード ← 音が出ない原因の本命'}",
    ]
    player = _player(interaction)
    vc = player.voice
    if not vc or not vc.is_connected():
        lines.append("VC未接続のため、テスト音は再生できませんでした（自分が入っているVCで /再生 を1回使ってから実行して）")
        await interaction.followup.send("\n".join(lines), ephemeral=True)
        return
    state = getattr(vc, "_connection", None)
    dave = getattr(state, "dave_session", None) if state is not None else None
    can = getattr(state, "can_encrypt", None) if state is not None else None
    lines.append(f"DAVEセッション: {'あり' if dave else 'なし'} / 暗号化可: {can}")
    if vc.is_playing() or vc.is_paused():
        lines.append("曲の再生中だったため、テスト音は見送りました（停止してから実行するとテスト音が出ます）")
        await interaction.followup.send("\n".join(lines), ephemeral=True)
        return
    # テスト音（880Hz・2秒）をその場で生成して再生する
    import asyncio as _aio
    import tempfile
    from pathlib import Path
    tone = Path(tempfile.gettempdir()) / "super_music_tone.wav"
    try:
        proc = await _aio.create_subprocess_exec(
            FFMPEG, "-y", "-f", "lavfi", "-i", "sine=frequency=880:duration=2",
            str(tone),
            stdout=_aio.subprocess.DEVNULL, stderr=_aio.subprocess.DEVNULL)
        await proc.wait()
        vc.play(discord.FFmpegPCMAudio(str(tone), executable=FFMPEG, options="-vn"))
        lines.append("テスト音（ピーッと2秒）を再生しました。聞こえましたか？")
    except Exception as e:
        lines.append(f"テスト音の再生に失敗: {e}")
    await interaction.followup.send("\n".join(lines), ephemeral=True)


@bot.tree.command(name="ヘルプ", description="音楽BOTの使い方を表示します")
async def cmd_help(interaction: discord.Interaction):
    text = (
        "**🎵 超高機能音楽BOTの使い方**\n"
        "`/再生 <曲名 or URL>` 再生 / キュー追加（YouTube・SoundCloud・Spotify対応。"
        "YouTubeが取得できない時は SoundCloud へ自動で切り替わります）\n"
        "`/パネル設置` セレクト1つで操作できる常設パネルを設置\n"
        "`/一時停止` `/再開` `/スキップ` `/停止` 再生コントロール\n"
        "`/キュー` `/キュー削除` `/全消去` `/シャッフル` キュー管理\n"
        "`/ループ` ループ切替 ｜ `/音量` 0-200 ｜ `/フィルター` 低音ブースト等\n"
        "`/お気に入り` よく聴く曲を保存 ｜ `/マイリスト` 自分専用プレイリスト\n"
        "`/履歴` 最近再生した曲 ｜ `/設定` サーバー設定（管理者） ｜ `/診断` 音源の状態確認（管理者） ｜ `/音声診断` 音が出ない時の確認 ｜ `/接続` BOTだけ先にVCへ入れる"
    )
    await interaction.response.send_message(text, ephemeral=True)


# ---------------------------------------------------------------- VC退出検知
@bot.event
async def on_voice_state_update(member: discord.Member, before, after):
    """人が全員抜けたら、設定秒数後にBOTも退出する（常駐モード時は除く）。"""
    if member.bot:
        return
    from player import PLAYERS, schedule_alone_disconnect, cancel_alone_disconnect
    for player in list(PLAYERS.values()):
        if player.voice and player.voice.is_connected() and player.voice.channel:
            humans = [m for m in player.voice.channel.members if not m.bot]
            if not humans:
                schedule_alone_disconnect(bot, player)
            else:
                cancel_alone_disconnect(player)
