# -*- coding: utf-8 -*-
"""
panel.py — 常設パネルと各種セレクトView

設計ルール（厳守）:
- UIは全てセレクトメニュー式。常設パネルはセレクト1つだけ。
- パネルは timeout=None + 固定custom_id で永久使用可能にし、
  setup_hook で add_view 登録、設置場所は store に保存して
  on_ready で自動再描画する。
- コールバックでは先に defer（is_done確認）、編集は defer 後。
  期限切れの discord.NotFound は静かに無視する。
- 再描画ヘルパーの名前は _redraw に統一する（旧名は使わない）。
- view を渡さない時は引数ごと省略する（Noneは渡さない）。
- Interaction に属性を追加して状態を渡さない（状態はView自身が持つ）。
"""

from typing import Optional

import discord
from discord.ext import commands

import store
from core import MAX_QUEUE_SHOW, FILTERS, FILTER_ORDER, filter_label
from player import (GuildPlayer, PLAYERS, get_player, ensure_voice,
                    add_query, play_next, disconnect_player, set_volume,
                    cycle_loop, LOOP_TEXT, can_control)


# ---------------------------------------------------------------- Embed
def panel_embed(player: GuildPlayer) -> discord.Embed:
    embed = discord.Embed(title="🎵 音楽パネル", color=0x5865F2)
    if player.current:
        embed.description = (
            f"**再生中**\n{player.current.title}\n"
            f"⏱ {player.current.duration_text()} ｜ リクエスト: {player.current.requester}")
    else:
        embed.description = "再生中の曲はありません。\n下のメニューから「曲を追加」してください。"
    embed.add_field(name="ループ", value=LOOP_TEXT[player.loop_mode], inline=True)
    embed.add_field(name="音量", value=f"{player.volume}%", inline=True)
    embed.add_field(name="フィルター", value=filter_label(player.filter_key), inline=True)
    embed.add_field(name="待機中", value=f"{len(player.queue)} 曲", inline=True)
    if player.queue:
        lines = [f"{i + 1}. {t.title}" for i, t in enumerate(player.queue[:5])]
        if len(player.queue) > 5:
            lines.append(f"…ほか {len(player.queue) - 5} 曲")
        embed.add_field(name="次の曲", value="\n".join(lines), inline=False)
    return embed


def queue_embed(player: GuildPlayer) -> discord.Embed:
    embed = discord.Embed(title="📜 キュー", color=0x5865F2)
    if player.current:
        embed.add_field(name="再生中",
                        value=f"{player.current.title}（{player.current.duration_text()}）",
                        inline=False)
    if player.queue:
        lines = [f"{i + 1}. {t.title}（{t.duration_text()}）"
                 for i, t in enumerate(player.queue[:MAX_QUEUE_SHOW])]
        if len(player.queue) > MAX_QUEUE_SHOW:
            lines.append(f"…ほか {len(player.queue) - MAX_QUEUE_SHOW} 曲")
        embed.add_field(name="待機中", value="\n".join(lines), inline=False)
    if not player.current and not player.queue:
        embed.description = "キューは空です。"
    embed.set_footer(text=f"ループ: {LOOP_TEXT[player.loop_mode]} ｜ 音量: {player.volume}% ｜ フィルター: {filter_label(player.filter_key)}")
    return embed


# ---------------------------------------------------------------- パネル再描画
async def _redraw(bot: commands.Bot, player: GuildPlayer):
    """設置済みパネルを現在の状態で描き直す。消えていたら設置記録を消す。"""
    loc = store.panel_locations().get(str(player.guild_id))
    if not loc:
        return
    try:
        channel = bot.get_channel(loc["channel"])
        if channel is None:
            channel = await bot.fetch_channel(loc["channel"])
        message = await channel.fetch_message(loc["message"])
        await message.edit(embed=panel_embed(player), view=MusicPanelView(bot))
    except discord.NotFound:
        store.drop_panel(player.guild_id)
    except Exception:
        pass


# 別名（他モジュールからの呼び出し名を統一するため）
redraw_panel = _redraw


async def redraw_all_panels(bot: commands.Bot):
    """on_ready 用: 保存されている全パネルを再描画する。"""
    for gid in store.panel_locations():
        await _redraw(bot, get_player(int(gid)))


# ---------------------------------------------------------------- 汎用: 安全な defer
async def _safe_defer(interaction: discord.Interaction):
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)


# ---------------------------------------------------------------- 曲追加モーダル
class AddSongModal(discord.ui.Modal, title="曲を追加"):
    query = discord.ui.TextInput(
        label="曲名 / YouTube・SoundCloud URL / Spotifyリンク",
        placeholder="例: YOASOBI アイドル",
        max_length=300,
    )

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await _safe_defer(interaction)
        player = get_player(interaction.guild_id)
        try:
            await ensure_voice(player, interaction.user)
            added, note = await add_query(
                self.bot, player, str(self.query.value),
                interaction.user.display_name, text_channel_id=interaction.channel_id)
        except Exception as e:
            try:
                await interaction.followup.send(f"❌ {e}", ephemeral=True)
            except discord.NotFound:
                pass
            return
        msg = f"✅ 追加しました: **{added[0].title}**" if len(added) == 1 \
            else f"✅ {len(added)}曲を追加しました"
        if note:
            msg += f"\n{note}"
        try:
            await interaction.followup.send(msg, ephemeral=True)
        except discord.NotFound:
            pass


# ---------------------------------------------------------------- フィルター選択
class FilterView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=180)
        self.bot = bot
        select = discord.ui.Select(
            placeholder="フィルターを選んでください（次曲から適用）",
            options=[discord.SelectOption(label=FILTERS[k]["label"], value=k,
                                          default=False)
                     for k in FILTER_ORDER])
        select.callback = self._on_pick
        self.add_item(select)

    async def _on_pick(self, interaction: discord.Interaction):
        await _safe_defer(interaction)
        player = get_player(interaction.guild_id)
        player.filter_key = interaction.data["values"][0]
        try:
            await interaction.followup.send(
                f"🎛 フィルターを **{filter_label(player.filter_key)}** にしました（次曲から適用）",
                ephemeral=True)
        except discord.NotFound:
            pass
        await _redraw(self.bot, player)


# ---------------------------------------------------------------- 音量選択
class VolumeView(discord.ui.View):
    PRESETS = [0, 25, 50, 75, 100, 125, 150, 175, 200]

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=180)
        self.bot = bot
        select = discord.ui.Select(
            placeholder="音量を選んでください（0-200%）",
            options=[discord.SelectOption(label=f"{v}%", value=str(v))
                     for v in self.PRESETS])
        select.callback = self._on_pick
        self.add_item(select)

    async def _on_pick(self, interaction: discord.Interaction):
        await _safe_defer(interaction)
        player = get_player(interaction.guild_id)
        applied = set_volume(player, int(interaction.data["values"][0]))
        try:
            await interaction.followup.send(f"🔊 音量を {applied}% にしました", ephemeral=True)
        except discord.NotFound:
            pass
        await _redraw(self.bot, player)


# ---------------------------------------------------------------- キュー削除選択
class QueueRemoveView(discord.ui.View):
    def __init__(self, bot: commands.Bot, player: GuildPlayer):
        super().__init__(timeout=180)
        self.bot = bot
        options = [discord.SelectOption(
            label=f"{i + 1}. {t.title}"[:100], value=str(i))
            for i, t in enumerate(player.queue[:25])]
        select = discord.ui.Select(placeholder="削除する曲を選んでください", options=options)
        select.callback = self._on_pick
        self.add_item(select)

    async def _on_pick(self, interaction: discord.Interaction):
        await _safe_defer(interaction)
        player = get_player(interaction.guild_id)
        removed = player.remove_at(int(interaction.data["values"][0]))
        try:
            if removed:
                await interaction.followup.send(f"🗑 キューから **{removed.title}** を削除しました", ephemeral=True)
            else:
                await interaction.followup.send("その曲は既にキューにありません", ephemeral=True)
        except discord.NotFound:
            pass
        await _redraw(self.bot, player)


# ---------------------------------------------------------------- 汎用: 曲ピック
class TrackPickView(discord.ui.View):
    """曲リストから1つ選ばせる汎用View。handler(interaction, index) を呼ぶ。"""

    def __init__(self, bot: commands.Bot, tracks: list, handler, placeholder: str):
        super().__init__(timeout=180)
        self.bot = bot
        self.handler = handler
        options = [discord.SelectOption(
            label=(f"{i + 1}. {t.get('title', '（タイトル不明）')}")[:100], value=str(i))
            for i, t in enumerate(tracks[:25])]
        select = discord.ui.Select(placeholder=placeholder, options=options)
        select.callback = self._on_pick
        self.add_item(select)

    async def _on_pick(self, interaction: discord.Interaction):
        await _safe_defer(interaction)
        await self.handler(interaction, int(interaction.data["values"][0]))


class NamePickView(discord.ui.View):
    """名前リスト（マイリスト名など）から1つ選ばせる汎用View。"""

    def __init__(self, bot: commands.Bot, names: list, handler, placeholder: str):
        super().__init__(timeout=180)
        self.bot = bot
        self.handler = handler
        options = [discord.SelectOption(label=n[:100], value=n) for n in names[:25]]
        select = discord.ui.Select(placeholder=placeholder, options=options)
        select.callback = self._on_pick
        self.add_item(select)

    async def _on_pick(self, interaction: discord.Interaction):
        await _safe_defer(interaction)
        await self.handler(interaction, interaction.data["values"][0])


# ---------------------------------------------------------------- 設定
class SettingValueModal(discord.ui.Modal, title="設定値を入力"):
    value = discord.ui.TextInput(label="値", max_length=20)

    def __init__(self, bot: commands.Bot, guild_id: int, key: str, label: str):
        super().__init__()
        self.bot = bot
        self.guild_id = guild_id
        self.key = key
        self.value.label = label

    async def on_submit(self, interaction: discord.Interaction):
        await _safe_defer(interaction)
        raw = str(self.value.value).strip()
        try:
            if self.key == "dj_role_id":
                if raw == "":
                    store.set_guild_config(self.guild_id, dj_role_id=None)
                    msg = "DJロールを解除しました（全員操作可）"
                else:
                    role_id = int(raw.strip("<@&>"))
                    store.set_guild_config(self.guild_id, dj_role_id=role_id)
                    msg = f"DJロールを <@&{role_id}> に設定しました"
            elif self.key == "default_volume":
                v = max(0, min(200, int(raw)))
                store.set_guild_config(self.guild_id, default_volume=v)
                get_player(self.guild_id).volume = v
                msg = f"デフォルト音量を {v}% にしました（次回起動時から初期値）"
            elif self.key in ("idle_seconds", "alone_seconds"):
                v = max(0, int(raw))
                store.set_guild_config(self.guild_id, **{self.key: v})
                label = "無再生時の自動退出" if self.key == "idle_seconds" else "BOTだけ残った時の自動退出"
                msg = f"{label}を {v} 秒にしました"
            else:
                msg = "不明な設定項目です"
        except ValueError:
            msg = "❌ 数字で入力してください（DJロールはロールID、空欄で解除）"
        try:
            await interaction.followup.send(msg, ephemeral=True)
        except discord.NotFound:
            pass


class SettingsView(discord.ui.View):
    """ /設定 の入口。項目を選ぶとモーダル or 即時トグル。"""

    ITEMS = [
        ("dj_role_id", "DJロール（ID入力・空欄で解除）"),
        ("default_volume", "デフォルト音量（0-200）"),
        ("idle_seconds", "無再生時の自動退出（秒）"),
        ("alone_seconds", "BOTだけ残った時の自動退出（秒）"),
        ("stay_247", "24時間常駐モード ON/OFF"),
        ("announce", "再生開始のお知らせ ON/OFF"),
    ]

    def __init__(self, bot: commands.Bot, guild_id: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.guild_id = guild_id
        cfg = store.guild_config(guild_id)
        options = []
        for key, label in self.ITEMS:
            desc = ""
            if key == "dj_role_id":
                desc = f"現在: {cfg.get('dj_role_id') or '未設定'}"
            elif key in ("default_volume", "idle_seconds", "alone_seconds"):
                desc = f"現在: {cfg.get(key)}"
            elif key == "stay_247":
                desc = f"現在: {'ON' if cfg.get('stay_247') else 'OFF'}"
            elif key == "announce":
                desc = f"現在: {'ON' if cfg.get('announce') else 'OFF'}"
            options.append(discord.SelectOption(label=label, value=key, description=desc[:100]))
        select = discord.ui.Select(placeholder="変更する設定を選んでください", options=options)
        select.callback = self._on_pick
        self.add_item(select)

    async def _on_pick(self, interaction: discord.Interaction):
        key = interaction.data["values"][0]
        if key in ("stay_247", "announce"):
            await _safe_defer(interaction)
            cfg = store.guild_config(self.guild_id)
            new_val = not cfg.get(key, False)
            store.set_guild_config(self.guild_id, **{key: new_val})
            label = "24時間常駐モード" if key == "stay_247" else "再生開始のお知らせ"
            try:
                await interaction.followup.send(
                    f"⚙️ {label} を **{'ON' if new_val else 'OFF'}** にしました", ephemeral=True)
            except discord.NotFound:
                pass
            return
        labels = dict(self.ITEMS)
        # モーダルは defer 前に返す必要があるため、未応答のまま send_modal する
        if not interaction.response.is_done():
            await interaction.response.send_modal(
                SettingValueModal(self.bot, self.guild_id, key, labels[key]))


# ---------------------------------------------------------------- 常設パネル
class MusicPanelView(discord.ui.View):
    """常設パネル。セレクト1つで全操作を行う。timeout=None + 固定custom_id。"""

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot
        select = discord.ui.Select(
            custom_id="super_music:panel_main",
            placeholder="🎵 操作を選んでください",
            options=[
                discord.SelectOption(label="曲を追加", value="add", emoji="➕",
                                     description="曲名・URL・Spotifyリンクを入力"),
                discord.SelectOption(label="一時停止 / 再開", value="toggle_pause", emoji="⏯️"),
                discord.SelectOption(label="スキップ", value="skip", emoji="⏭️"),
                discord.SelectOption(label="停止", value="stop", emoji="⏹️"),
                discord.SelectOption(label="ループ切替", value="loop", emoji="🔁"),
                discord.SelectOption(label="シャッフル", value="shuffle", emoji="🔀"),
                discord.SelectOption(label="キュー表示", value="queue_show", emoji="📜"),
                discord.SelectOption(label="キューから削除", value="queue_remove", emoji="🗑️"),
                discord.SelectOption(label="再生中の曲をお気に入りに追加", value="fav_add", emoji="⭐"),
                discord.SelectOption(label="フィルター変更", value="filter", emoji="🎛️"),
                discord.SelectOption(label="音量変更", value="volume", emoji="🔊"),
            ])
        select.callback = self._on_action
        self.add_item(select)

    async def _on_action(self, interaction: discord.Interaction):
        action = interaction.data["values"][0]
        # 曲追加はモーダルが最初の応答でなければならないので defer しない
        if action == "add":
            if not interaction.response.is_done():
                await interaction.response.send_modal(AddSongModal(self.bot))
            return
        await _safe_defer(interaction)
        player = get_player(interaction.guild_id)

        async def say(text: str, **kwargs):
            try:
                await interaction.followup.send(text, ephemeral=True, **kwargs)
            except discord.NotFound:
                pass

        if not can_control(interaction):
            await say("この操作はDJロール（または管理者）が必要です")
            return

        if action == "toggle_pause":
            if player.voice and player.voice.is_playing():
                player.voice.pause()
                await say("⏸ 一時停止しました")
            elif player.voice and player.voice.is_paused():
                player.voice.resume()
                await say("▶️ 再開しました")
            else:
                await say("再生中の曲がありません")
        elif action == "skip":
            if player.voice and (player.voice.is_playing() or player.voice.is_paused()):
                player.voice.stop()
                await say("⏭ スキップしました")
            else:
                await say("再生中の曲がありません")
        elif action == "stop":
            await disconnect_player(self.bot, player)
            await say("⏹ 停止して退出しました")
        elif action == "loop":
            mode = cycle_loop(player)
            await say(f"🔁 ループ設定: {LOOP_TEXT[mode]}")
            await _redraw(self.bot, player)
        elif action == "shuffle":
            if player.queue:
                player.shuffle_queue()
                await say("🔀 キューをシャッフルしました")
                await _redraw(self.bot, player)
            else:
                await say("キューが空です")
        elif action == "queue_show":
            try:
                await interaction.followup.send(embed=queue_embed(player), ephemeral=True)
            except discord.NotFound:
                pass
        elif action == "queue_remove":
            if not player.queue:
                await say("キューが空です")
            else:
                try:
                    await interaction.followup.send(
                        "削除する曲を選んでください",
                        view=QueueRemoveView(self.bot, player), ephemeral=True)
                except discord.NotFound:
                    pass
        elif action == "fav_add":
            if not player.current:
                await say("再生中の曲がありません")
            else:
                added = store.add_favorite(interaction.user.id, player.current.to_dict())
                await say("⭐ お気に入りに追加しました" if added else "既にお気に入りに入っています")
        elif action == "filter":
            try:
                await interaction.followup.send(
                    "フィルターを選んでください",
                    view=FilterView(self.bot), ephemeral=True)
            except discord.NotFound:
                pass
        elif action == "volume":
            try:
                await interaction.followup.send(
                    "音量を選んでください",
                    view=VolumeView(self.bot), ephemeral=True)
            except discord.NotFound:
                pass
