# -*- coding: utf-8 -*-
"""
commands.py — スラッシュコマンド v2

/setup_panel /join /leave /skip /voice /word_add /word_remove /word_list
/mute_tts /ttstest /voicediag /ttshelp
診断コマンド（/ttstest・/voicediag）は v1 で後付けしたものを最初から内蔵。
/voicediag はテスト音ファイルを再生後に削除する。
"""

import discord
from discord import app_commands

from core import bot, store, is_privileged, VOICES, user_setting, TMP_DIR
from panel import install_panel
from speech import get_session, skip_current, _wait_encrypt


def _deny(interaction: discord.Interaction):
    return interaction.response.send_message("この操作は管理者のみです", ephemeral=True)


@bot.tree.command(name="setup_panel", description="読み上げ設定パネルをこのチャンネルに設置します（管理者用）")
async def cmd_setup_panel(interaction: discord.Interaction):
    if not is_privileged(interaction.user):
        await _deny(interaction)
        return
    cfg = store.cfg(interaction.guild.id)
    old = cfg.get("panel")
    if old:
        try:
            ch = interaction.guild.get_channel(old["channel"])
            if ch is not None:
                msg = await ch.fetch_message(old["message"])
                await msg.delete()
        except Exception:
            pass
    await install_panel(interaction.guild, interaction.channel)
    await interaction.response.send_message("🔊 読み上げパネルを設置しました", ephemeral=True)


@bot.tree.command(name="join", description="ボイスチャンネルに入室します（管理者用）")
async def cmd_join(interaction: discord.Interaction):
    if not is_privileged(interaction.user):
        await _deny(interaction)
        return
    from resident import manual_join
    try:
        channel = await manual_join(interaction.guild, interaction.user)
        await interaction.response.send_message(f"▶️ {channel.name} に入室しました", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"入室できませんでした: {e}", ephemeral=True)


@bot.tree.command(name="leave", description="ボイスチャンネルから退出します（管理者用）")
async def cmd_leave(interaction: discord.Interaction):
    if not is_privileged(interaction.user):
        await _deny(interaction)
        return
    from resident import leave
    await leave(interaction.guild)
    await interaction.response.send_message("⏹ 退出しました", ephemeral=True)


@bot.tree.command(name="skip", description="今の読み上げをスキップします")
async def cmd_skip(interaction: discord.Interaction):
    session = get_session(interaction.guild.id)
    skip_current(session)
    await interaction.response.send_message("⏭ スキップしました", ephemeral=True)


class VoiceSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        options = [discord.SelectOption(label="サーバーのデフォルトに戻す", value="default")]
        options += [discord.SelectOption(label=name, value=key) for key, (_, name) in VOICES.items()]
        select = discord.ui.Select(placeholder="🎙 自分の読み上げの声を選ぶ", options=options)
        select.callback = self._cb
        self.add_item(select)

    async def _cb(self, interaction: discord.Interaction):
        cfg = store.cfg(interaction.guild.id)
        setting = user_setting(cfg, interaction.user.id)
        value = interaction.data["values"][0]
        setting["voice"] = None if value == "default" else value
        store.save()
        label = "サーバーのデフォルト" if value == "default" else VOICES[value][1]
        await interaction.response.edit_message(content=f"🎙 あなたの声を {label} にしました", view=None)


@bot.tree.command(name="voice", description="自分の発言を読み上げる声を変更します")
async def cmd_voice(interaction: discord.Interaction):
    await interaction.response.send_message("読み上げの声を選んでください", view=VoiceSelectView(), ephemeral=True)


@bot.tree.command(name="word_add", description="単語の読み方を辞書に登録します（管理者用）")
@app_commands.describe(word="単語", reading="読み方（ひらがな推奨）")
async def cmd_word_add(interaction: discord.Interaction, word: str, reading: str):
    if not is_privileged(interaction.user):
        await _deny(interaction)
        return
    cfg = store.cfg(interaction.guild.id)
    cfg["dictionary"][word] = reading
    store.save()
    await interaction.response.send_message(f"📝 「{word}」→「{reading}」を登録しました", ephemeral=True)


@bot.tree.command(name="word_remove", description="単語辞書から削除します（管理者用）")
@app_commands.describe(word="単語")
async def cmd_word_remove(interaction: discord.Interaction, word: str):
    if not is_privileged(interaction.user):
        await _deny(interaction)
        return
    cfg = store.cfg(interaction.guild.id)
    if cfg["dictionary"].pop(word, None) is None:
        await interaction.response.send_message("その単語は登録されていません", ephemeral=True)
    else:
        store.save()
        await interaction.response.send_message(f"🗑 「{word}」を削除しました", ephemeral=True)


@bot.tree.command(name="word_list", description="単語辞書の一覧を表示します")
async def cmd_word_list(interaction: discord.Interaction):
    cfg = store.cfg(interaction.guild.id)
    dictionary = cfg.get("dictionary", {})
    if not dictionary:
        await interaction.response.send_message("辞書は空です。/word_add で登録できます", ephemeral=True)
        return
    lines = [f"・{w} → {r}" for w, r in list(dictionary.items())[:50]]
    await interaction.response.send_message("📝 単語辞書\n" + "\n".join(lines), ephemeral=True)


@bot.tree.command(name="mute_tts", description="指定ユーザーの発言を読み上げ対象から外す/戻す（管理者用）")
@app_commands.describe(user="対象ユーザー")
async def cmd_mute_tts(interaction: discord.Interaction, user: discord.Member):
    if not is_privileged(interaction.user):
        await _deny(interaction)
        return
    cfg = store.cfg(interaction.guild.id)
    setting = user_setting(cfg, user.id)
    setting["muted"] = not setting.get("muted", False)
    store.save()
    state = "読み上げミュートにしました" if setting["muted"] else "ミュートを解除しました"
    await interaction.response.send_message(f"🔇 {user.display_name} を{state}", ephemeral=True)


@bot.tree.command(name="ttstest", description="切り分け用: テスト音声を生成してファイルで貼ります")
async def cmd_ttstest(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    from speech import synthesize
    cfg = store.cfg(interaction.guild.id)
    try:
        path = await synthesize(
            "テストです。この音声が聞こえれば、合成は正常です。",
            cfg.get("default_voice", "nanami"), cfg.get("rate", "+0%"),
        )
    except Exception as e:
        await interaction.followup.send(f"❌ 合成に失敗しました: {e}", ephemeral=True)
        return
    try:
        size = path.stat().st_size
        await interaction.followup.send(
            f"生成した音声ファイル（{size} bytes）です。再生して音が入っているか確認してください。\n"
            "・音が入っている → ファイルは正常。聞こえない原因はVC側（音量・端末）\n"
            "・無音 → 合成側の問題",
            file=discord.File(str(path)), ephemeral=True,
        )
    finally:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass


@bot.tree.command(name="voicediag", description="音声の診断（バージョン確認とテスト音の再生）")
async def cmd_voicediag(interaction: discord.Interaction):
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
        f"edge-tts: {ver('edge-tts')}",
    ]
    # v1 から移植（2026-11-13）: libopus と DAVE の状態が無いと、
    # 「暗号化可: False」が原因だと分からない。
    import discord.opus
    lines.append(f"libopus（音声エンコード）: {'ロード済み' if discord.opus.is_loaded() else '未ロード ← 音が出ない原因'}")
    session0 = get_session(interaction.guild.id)
    if session0.connected():
        vc = session0.voice
        state = getattr(vc, "_connection", None)
        dave = getattr(state, "dave_session", None) if state is not None else None
        can = getattr(state, "can_encrypt", None) if state is not None else None
        lines.append(f"DAVEセッション: {'あり' if dave else 'なし'} / 暗号化可: {can}")
        # 2026-11-13 追加: False のとき「どこで止まっているか」を分ける。
        # protocol=0 → サーバー側がDAVEを出していない / ready=False → 鍵交換
        # (MLSの welcome/commit) がまだ届いていない。どちらも False のまま
        # 再生すると、暗号化なしの音声は相手クライアントに捨てられて無音になる。
        if state is not None:
            lines.append(f"DAVEプロトコル: {getattr(state, 'dave_protocol_version', '?')}")
            lines.append(f"DAVE ready: {getattr(dave, 'ready', '-') if dave else '-'}")
            try:
                lines.append(f"davey can_encrypt: {dave.can_encrypt} / status: {dave.status}")
            except Exception:
                pass
    session = get_session(interaction.guild.id)
    if not session.connected():
        lines.append("VC未接続のため、テスト音は再生できませんでした（VCに入ってから実行して）")
        await interaction.followup.send("\n".join(lines), ephemeral=True)
        return
    import asyncio as _aio
    TMP_DIR.mkdir(exist_ok=True)
    tone = TMP_DIR / "tone_test.wav"
    try:
        proc = await _aio.create_subprocess_exec(
            "ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=880:duration=2",
            str(tone),
            stdout=_aio.subprocess.DEVNULL, stderr=_aio.subprocess.DEVNULL,
        )
        await proc.wait()
        # テスト音を鳴らす前に暗号化の完了を最大10秒待つ。
        # 待っても未完了なら、そのまま鳴らしても相手には届かない見込み。
        encrypt_ok = await _wait_encrypt(session, timeout=10.0)
        lines.append(f"暗号化待ち(10秒): {'完了' if encrypt_ok else '未完了のまま'}")
        if session.voice.is_playing():
            lines.append("再生中だったため、テスト音は見送りました")
        else:
            session.voice.play(discord.FFmpegPCMAudio(str(tone), options="-vn"))
            lines.append("テスト音（ピーッと2秒）を再生しました。聞こえましたか？")
    except Exception as e:
        lines.append(f"テスト音の再生に失敗: {e}")
    finally:
        # 再生が終わるまで少し待ってから削除（削除し忘れ防止）
        async def _cleanup():
            await _aio.sleep(5)
            try:
                tone.unlink(missing_ok=True)
            except Exception:
                pass
        _aio.create_task(_cleanup())
    await interaction.followup.send("\n".join(lines), ephemeral=True)


@bot.tree.command(name="timer", description="指定した分後にVCで読み上げます（タイマー）")
@app_commands.describe(minutes="何分後か（1〜720）", text="読み上げる内容（省略時は「時間になりました」）")
async def cmd_timer(interaction: discord.Interaction, minutes: int, text: str = ""):
    import time as _time
    import timers as timers_mod
    if not (1 <= minutes <= 720):
        await interaction.response.send_message("⏰ 分は1〜720で指定してください", ephemeral=True)
        return
    fire_at = _time.time() + minutes * 60
    timers_mod.add_timer(interaction.guild.id, fire_at,
                         (text or "").strip(), interaction.user.id,
                         interaction.user.display_name)
    await interaction.response.send_message(
        f"⏰ {minutes}分後にタイマーをセットしました"
        + (f"（内容: {text.strip()}）" if text.strip() else ""),
        ephemeral=True)


@bot.tree.command(name="timers", description="予約中のタイマー一覧を表示します")
async def cmd_timers(interaction: discord.Interaction):
    import time as _time
    import datetime as _dt
    import timers as timers_mod
    items = timers_mod.list_timers(interaction.guild.id)
    if not items:
        await interaction.response.send_message("⏰ 予約中のタイマーはありません。/timer でセットできます", ephemeral=True)
        return
    now = _time.time()
    lines = []
    for i, t in enumerate(items):
        remain = max(0, int(t["fire_at"] - now))
        when = _dt.datetime.fromtimestamp(t["fire_at"]).strftime("%H:%M")
        lines.append(f"{i + 1}. {when}（あと約{remain // 60}分） {t.get('text') or '時間になりました'}"
                     f"（{t.get('user_name', '?')}）")
    await interaction.response.send_message(
        "⏰ **タイマー一覧**\n" + "\n".join(lines) + "\n取消は /timer_cancel 番号", ephemeral=True)


@bot.tree.command(name="timer_cancel", description="タイマーを取り消します")
@app_commands.describe(number="一覧の番号")
async def cmd_timer_cancel(interaction: discord.Interaction, number: int):
    import timers as timers_mod
    items = timers_mod.list_timers(interaction.guild.id)
    if not (1 <= number <= len(items)):
        await interaction.response.send_message("その番号のタイマーはありません", ephemeral=True)
        return
    target = items[number - 1]
    if target.get("user_id") != interaction.user.id and not is_privileged(interaction.user):
        await interaction.response.send_message("他人のタイマーは取り消せません", ephemeral=True)
        return
    removed = timers_mod.cancel_timer(interaction.guild.id, number - 1)
    await interaction.response.send_message(
        f"🗑 タイマーを取り消しました: {removed.get('text') or '時間になりました'}", ephemeral=True)


@bot.tree.command(name="ttshelp", description="読み上げBOTの使い方を表示します")
async def cmd_help(interaction: discord.Interaction):
    text = (
        "**🔊 読み上げBOTの使い方**\n"
        "誰かがVCに入ると自動で入室し、設定したテキストチャンネルの発言を読み上げます。全員出たら自動で退出します。\n"
        "`/setup_panel` 設定パネルを設置（設定は全部パネルのセレクトメニューから）\n"
        "`/voice` 自分の声を変更 ｜ `/skip` 今の読み上げを飛ばす\n"
        "`/word_add` `/word_remove` `/word_list` 単語辞書\n"
        "`/join` `/leave` 手動の入室・退出 ｜ `/mute_tts` 特定ユーザーを読み上げ対象外に\n"
        "`/timer` ◯分後に読み上げ ｜ `/timers` 一覧 ｜ `/timer_cancel` 取消\n"
        "`/voicediag` 音声が聞こえない時の診断 ｜ `/ttstest` 合成の確認\n"
        "※ 開発者ポータルで MESSAGE CONTENT INTENT を ON にしてください。"
    )
    await interaction.response.send_message(text, ephemeral=True)
