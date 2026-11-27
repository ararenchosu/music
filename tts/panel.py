# -*- coding: utf-8 -*-
"""
panel.py — 設定パネル v2（全部セレクトメニュー式）

- 読み上げチャンネル / 対象VC / デフォルトの声 / 各種ON-OFF / 操作 を
  すべてセレクトメニューで変更する（ボタンは使わない）
- パネルは設置場所を保存し、起動時に自動で最新構成へ更新する
- 設定の変更は 管理者 または 信頼ユーザー のみ
- v2の変更（作り直しの中心）:
  (1) 全セレクトのコールバックが最初に defer する。重い処理
      （VC接続など）の前に「考え中」を返すので、Discordの3秒期限切れ
      (Unknown interaction 10062) にならない。
  (2) 永続パネルをサーバーごとに登録する。v1は guild=None の
      1枚だけを登録していたため、再起動後にVC一覧つきパネルが
      復活しなかった。on_ready で各サーバー用の View を登録する。
  (3) パネル更新時、キャッシュに無いチャンネルは fetch で取りに行く。
"""

import discord

from core import (bot, store, is_privileged, VOICES, RATES, RATE_LABELS,
                  MAX_CHARS_CHOICES)
from speech import get_session, skip_current, clear_queue


def panel_embed(guild: discord.Guild) -> discord.Embed:
    cfg = store.cfg(guild.id)
    session = get_session(guild.id)
    embed = discord.Embed(title="🔊 読み上げ設定パネル", color=0x5865F2)
    if session.connected():
        embed.add_field(name="接続中VC", value=session.voice.channel.name, inline=True)
    else:
        embed.add_field(name="接続中VC", value="未接続", inline=True)
    read_ch = guild.get_channel(cfg.get("read_channel_id") or 0)
    embed.add_field(name="読み上げチャンネル",
                    value=read_ch.mention if read_ch else "未設定", inline=True)
    vc = guild.get_channel(cfg.get("target_vc_id") or 0)
    embed.add_field(name="対象VC", value=vc.name if vc else "自動（人がいるVC）", inline=True)
    embed.add_field(name="読み上げ", value="ON" if cfg.get("enabled", True) else "OFF", inline=True)
    embed.add_field(name="常駐モード", value="ON" if cfg.get("stay_247") else "OFF", inline=True)
    embed.add_field(name="名前読み上げ", value="ON" if cfg.get("read_name", True) else "OFF", inline=True)
    embed.add_field(name="あいさつ", value="ON" if cfg.get("greetings", True) else "OFF", inline=True)
    embed.add_field(name="読み上げ対象",
                    value="VC参加者のみ" if cfg.get("only_vc_members") else "全員", inline=True)
    embed.add_field(name="VC内チャット",
                    value="読む" if cfg.get("read_vc_chat", True) else "読まない", inline=True)
    embed.add_field(name="デフォルトの声",
                    value=VOICES.get(cfg.get("default_voice", "nanami"), ("", "?"))[1], inline=True)
    embed.add_field(name="速度", value=RATE_LABELS.get(cfg.get("rate", "+0%"), "普通"), inline=True)
    embed.add_field(name="最大文字数", value=f"{cfg.get('max_chars', 100)}文字", inline=True)
    embed.add_field(name="単語辞書", value=f"{len(cfg.get('dictionary', {}))}件", inline=True)
    q = session.queue.qsize()
    embed.add_field(name="読み上げ待ち", value=f"{q}件", inline=True)
    embed.set_footer(text="設定は下のセレクトメニューから（管理者のみ変更可）")
    return embed


class PanelView(discord.ui.View):
    """常設パネル。custom_id 固定 + timeout=None で再起動後も効く。"""

    def __init__(self, bot_, guild):
        super().__init__(timeout=None)
        self.bot = bot_

        ch_select = discord.ui.ChannelSelect(
            custom_id="tts:set_channel",
            placeholder="📺 読み上げするテキストチャンネルを選ぶ",
            channel_types=[discord.ChannelType.text],
            min_values=1, max_values=1, row=0,
        )
        ch_select.callback = self._cb_channel
        self.add_item(ch_select)

        vc_options = [discord.SelectOption(label="🤖 自動（人がいるVC）", value="auto")]
        if guild is not None:
            for ch in guild.voice_channels[:24]:
                vc_options.append(discord.SelectOption(label=f"🔊 {ch.name}"[:100], value=str(ch.id)))
        vc_select = discord.ui.Select(
            custom_id="tts:set_vc", placeholder="🔊 常駐するボイスチャンネルを選ぶ",
            options=vc_options, min_values=1, max_values=1, row=1,
        )
        vc_select.callback = self._cb_vc
        self.add_item(vc_select)

        voice_select = discord.ui.Select(
            custom_id="tts:set_voice", placeholder="🎙 デフォルトの声を選ぶ",
            options=[discord.SelectOption(label=name, value=key) for key, (_, name) in VOICES.items()],
            min_values=1, max_values=1, row=2,
        )
        voice_select.callback = self._cb_voice
        self.add_item(voice_select)

        toggle_select = discord.ui.Select(
            custom_id="tts:toggle", placeholder="⚙️ 設定の切り替え",
            options=[
                discord.SelectOption(label="📢 読み上げ ON/OFF", value="enabled"),
                discord.SelectOption(label="🏷 名前読み上げ ON/OFF", value="read_name"),
                discord.SelectOption(label="👋 あいさつ ON/OFF", value="greetings"),
                discord.SelectOption(label="🎧 読み上げ対象（VC参加者のみ ⇔ 全員）", value="only_vc"),
                discord.SelectOption(label="💬 VC内チャット読み上げ ON/OFF", value="read_vc_chat"),
                discord.SelectOption(label="🏠 常駐モード ON/OFF（退出しない・自動復帰）", value="stay_247"),
                discord.SelectOption(label="📝 最大文字数を切り替え（50→100→200→300）", value="max_chars"),
                discord.SelectOption(label="🏃 読み上げ速度を切り替え", value="rate"),
            ],
            min_values=1, max_values=1, row=3,
        )
        toggle_select.callback = self._cb_toggle
        self.add_item(toggle_select)

        action_select = discord.ui.Select(
            custom_id="tts:action", placeholder="▶️ 操作",
            options=[
                discord.SelectOption(label="▶️ 今すぐ入室する", value="join"),
                discord.SelectOption(label="⏹ 退出する", value="leave"),
                discord.SelectOption(label="⏭ 今の読み上げをスキップ", value="skip"),
                discord.SelectOption(label="🧹 読み上げキューを消す", value="clear"),
                discord.SelectOption(label="🔄 パネル表示を更新", value="refresh"),
            ],
            min_values=1, max_values=1, row=4,
        )
        action_select.callback = self._cb_action
        self.add_item(action_select)

    # ---- 共通（全コールバックは defer 済みの前提で followup を使う）
    async def _gate(self, interaction: discord.Interaction) -> bool:
        if is_privileged(interaction.user):
            return True
        await interaction.followup.send("設定の変更は管理者のみです", ephemeral=True)
        return False

    async def _update(self, interaction: discord.Interaction, note: str):
        guild = interaction.guild
        await interaction.followup.send(note, ephemeral=True)
        if interaction.message is not None:
            try:
                await interaction.message.edit(embed=panel_embed(guild), view=PanelView(self.bot, guild))
            except Exception:
                pass

    async def _defer(self, interaction: discord.Interaction) -> bool:
        """最初に defer する。既に済みなら False（重複defer防止）。"""
        if interaction.response.is_done():
            return False
        try:
            await interaction.response.defer(ephemeral=True)
            return True
        except Exception:
            return False

    # ---- 各セレクトのコールバック（全て defer → 権限 → 処理 → followup）
    async def _cb_channel(self, interaction: discord.Interaction):
        await self._defer(interaction)
        if not await self._gate(interaction):
            return
        cfg = store.cfg(interaction.guild.id)
        resolved = interaction.data.get("resolved", {}).get("channels", {})
        ids = list(resolved.keys())
        if ids:
            cfg["read_channel_id"] = int(ids[0])
        elif interaction.data.get("values"):
            cfg["read_channel_id"] = int(interaction.data["values"][0])
        store.save()
        await self._update(interaction, "📺 読み上げチャンネルを設定しました")

    async def _cb_vc(self, interaction: discord.Interaction):
        await self._defer(interaction)
        if not await self._gate(interaction):
            return
        cfg = store.cfg(interaction.guild.id)
        value = interaction.data["values"][0]
        cfg["target_vc_id"] = None if value == "auto" else int(value)
        store.save()
        from resident import evaluate
        await evaluate(interaction.guild)
        await self._update(interaction, "🔊 対象VCを設定しました")

    async def _cb_voice(self, interaction: discord.Interaction):
        await self._defer(interaction)
        if not await self._gate(interaction):
            return
        cfg = store.cfg(interaction.guild.id)
        cfg["default_voice"] = interaction.data["values"][0]
        store.save()
        await self._update(interaction, "🎙 デフォルトの声を変更しました")

    async def _cb_toggle(self, interaction: discord.Interaction):
        await self._defer(interaction)
        if not await self._gate(interaction):
            return
        cfg = store.cfg(interaction.guild.id)
        key = interaction.data["values"][0]
        if key in ("enabled", "read_name", "greetings"):
            cfg[key] = not cfg.get(key, True)
            note = f"{key} を {'ON' if cfg[key] else 'OFF'} にしました"
        elif key == "read_vc_chat":
            cfg["read_vc_chat"] = not cfg.get("read_vc_chat", True)
            note = f"VC内チャットの読み上げを {'ON' if cfg['read_vc_chat'] else 'OFF'} にしました"
        elif key == "stay_247":
            cfg["stay_247"] = not cfg.get("stay_247", False)
            if cfg["stay_247"]:
                s = get_session(interaction.guild.id)
                if s.connected():
                    cfg["last_vc_id"] = s.voice.channel.id
            note = (f"常駐モードを {'ON' if cfg['stay_247'] else 'OFF'} にしました"
                    "（ONの間は人がいなくても退出せず、切断・再起動後も自動で戻ります）")
        elif key == "only_vc":
            cfg["only_vc_members"] = not cfg.get("only_vc_members", False)
            note = f"読み上げ対象を {'VC参加者のみ' if cfg['only_vc_members'] else '全員'} にしました"
        elif key == "max_chars":
            cur = cfg.get("max_chars", 100)
            idx = MAX_CHARS_CHOICES.index(cur) if cur in MAX_CHARS_CHOICES else 1
            cfg["max_chars"] = MAX_CHARS_CHOICES[(idx + 1) % len(MAX_CHARS_CHOICES)]
            note = f"最大文字数を {cfg['max_chars']}文字 にしました"
        elif key == "rate":
            cur = cfg.get("rate", "+0%")
            idx = RATES.index(cur) if cur in RATES else 1
            cfg["rate"] = RATES[(idx + 1) % len(RATES)]
            note = f"速度を {RATE_LABELS[cfg['rate']]} にしました"
        else:
            note = "変更しました"
        store.save()
        await self._update(interaction, note)

    async def _cb_action(self, interaction: discord.Interaction):
        await self._defer(interaction)
        if not await self._gate(interaction):
            return
        action = interaction.data["values"][0]
        guild = interaction.guild
        session = get_session(guild.id)
        if action == "join":
            from resident import manual_join
            try:
                channel = await manual_join(guild, interaction.user)
                note = f"▶️ {channel.name} に入室しました"
            except Exception as e:
                note = f"入室できませんでした: {e}"
        elif action == "leave":
            from resident import leave
            await leave(guild)
            note = "⏹ 退出しました"
        elif action == "skip":
            skip_current(session)
            note = "⏭ スキップしました"
        elif action == "clear":
            clear_queue(session)
            note = "🧹 キューを消しました"
        else:
            note = "🔄 更新しました"
        await self._update(interaction, note)


def build_panel_view(guild) -> PanelView:
    return PanelView(bot, guild)


async def install_panel(guild: discord.Guild, channel: discord.TextChannel):
    message = await channel.send(embed=panel_embed(guild), view=build_panel_view(guild))
    cfg = store.cfg(guild.id)
    cfg["panel"] = {"channel": channel.id, "message": message.id}
    store.save()
    return message


async def refresh_stored_panel(guild: discord.Guild):
    cfg = store.cfg(guild.id)
    panel = cfg.get("panel")
    if not panel:
        return
    try:
        channel = bot.get_channel(panel["channel"])
        if channel is None:
            channel = await bot.fetch_channel(panel["channel"])
        message = await channel.fetch_message(panel["message"])
        await message.edit(embed=panel_embed(guild), view=build_panel_view(guild))
    except Exception:
        cfg["panel"] = None
        store.save()


@bot.listen("on_ready")
async def _register_and_refresh_panels():
    """起動時: サーバーごとに永続パネルを登録し、設置済みパネルを最新化する。"""
    registered = getattr(bot, "_tts_panel_registered", False)
    for guild in bot.guilds:
        if not registered:
            try:
                bot.add_view(PanelView(bot, guild))
            except Exception:
                pass
        await refresh_stored_panel(guild)
    bot._tts_panel_registered = True
