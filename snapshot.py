import os
import io
import csv
import json
import requests
import discord
import time

from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# Google Sheets libraries
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# Load environment variables from .env
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
API_KEY = os.getenv("API_KEY")
SERVICE_ACCOUNT_INFO = os.getenv("SERVICE_ACCOUNT_INFO")

# Setup Google Sheets client
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_dict = json.loads(SERVICE_ACCOUNT_INFO)
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gc = gspread.authorize(creds)

# Attempt to open the spreadsheet and select the "log" sheet
sh = gc.open("snapshot_bot_log")
worksheet = sh.worksheet("log")  # For snapshot logs

intents = discord.Intents.default()
intents.members = True  # ★追加：ロール抽出に必要（Developer Portal で Server Members Intent をONに）
bot = commands.Bot(command_prefix="!", intents=intents)

# -------- モーダル (ウォレット入力) -------- #
class RegisterWalletModal(discord.ui.Modal):
    """
    This modal pops up when a user clicks the "Register your wallet" button.
    The user can type their wallet address. On submit, we check if it's
    already registered in 'wallet_log' or register a new one.
    """
    def __init__(self, spreadsheet):
        super().__init__(title="Register your wallet")

        # We'll store the spreadsheet reference to log data
        self.spreadsheet = spreadsheet

        # Text input field (required)
        self.wallet_input = discord.ui.TextInput(
            label="Wallet Address",
            placeholder="Enter your wallet address here",
            required=True,
            max_length=100
        )
        self.add_item(self.wallet_input)

    async def on_submit(self, interaction: discord.Interaction):
        # Retrieve user info
        user_name = str(interaction.user)
        user_id = str(interaction.user.id)
        wallet_address = self.wallet_input.value.strip()

        # Try to get 'wallet_log' sheet
        try:
            register_worksheet = self.spreadsheet.worksheet("wallet_log")
        except gspread.WorksheetNotFound:
            await interaction.response.send_message(
                content="The sheet 'wallet_log' was not found. Please create it first.",
                ephemeral=True
            )
            return

        # Check duplication (can handle ~1000 rows or more)
        all_values = register_worksheet.get_all_values()
        existing_row = None
        for row in all_values:
            # row = [DiscordName, DiscordID, WalletAddress]
            if len(row) >= 2 and row[1] == user_id:
                existing_row = row
                break

        if existing_row:
            # Already registered
            already_wallet = existing_row[2] if len(existing_row) > 2 else "N/A"
            await interaction.response.send_message(
                content=(
                    f"You are already registered.\n"
                    f"**Name**: {existing_row[0]}\n"
                    f"**Wallet**: {already_wallet}"
                ),
                ephemeral=True
            )
        else:
            # Not yet registered -> append a new row
            register_worksheet.append_row([user_name, user_id, wallet_address], value_input_option="RAW")
            await interaction.response.send_message(
                content=(
                    f"Your wallet has been registered.\n"
                    f"**Name**: {user_name}\n"
                    f"**Wallet**: {wallet_address}"
                ),
                ephemeral=True
            )

# -------- ボタン付きのView -------- #
class RegisterWalletView(discord.ui.View):
    """
    This View contains a button that opens the RegisterWalletModal.
    By default, the View doesn't timeout (timeout=None) so it stays active.
    """
    def __init__(self, spreadsheet):
        super().__init__(timeout=None)
        self.spreadsheet = spreadsheet

    @discord.ui.button(label="Register your wallet", style=discord.ButtonStyle.primary)
    async def register_wallet_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """
        When the user clicks this button, we show the modal (RegisterWalletModal).
        """
        modal = RegisterWalletModal(self.spreadsheet)
        await interaction.response.send_modal(modal)

# -------- Cog -------- #
class SnapshotCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="snapshot",
        description="Fetch token holder info for a contract address (ephemeral)."
    )
    @app_commands.describe(contract_address="Enter the token contract address")
    async def snapshot(self, interaction: discord.Interaction, contract_address: str):
        """
        Retrieve up to 1000 NFT holders.
        This may take a while if there are many holders.
        (API is called at 0.5-second intervals)
        """
        # Defer the response so it remains ephemeral until final output
        await interaction.response.defer(ephemeral=True)

        # Initial progress message
        progress_message = await interaction.followup.send(
            content="Fetching token holders... (page 1)",
            ephemeral=True
        )

        base_url = "https://api.socialscan.io/monad-testnet/v1/developer/api"
        module = "token"
        action = "tokenholderlist"
        offset = 100
        page = 1

        # 1) Collect all holder info
        all_holders = []

        # Stop if too many consecutive errors occur
        max_consecutive_errors = 5
        error_count = 0

        # Limit to 1000 holders
        max_holders = 1000

        while True:
            # Update progress
            await progress_message.edit(content=f"Now reading page {page}...")

            params = {
                "module": module,
                "action": action,
                "contractaddress": contract_address,
                "page": page,
                "offset": offset,
                "apikey": API_KEY
            }
            response = requests.get(base_url, params=params)

            # Handle errors with retry
            if response.status_code != 200:
                error_count += 1
                if error_count >= max_consecutive_errors:
                    break
                time.sleep(0.5)
                continue

            data = response.json()
            if data.get("status") != "1":
                error_count += 1
                if error_count >= max_consecutive_errors:
                    break
                time.sleep(0.5)
                continue
            else:
                error_count = 0  # reset on success

            result_list = data.get("result")
            if not result_list:
                # No data -> end
                break

            for holder in result_list:
                address = holder["TokenHolderAddress"]
                quantity_float = float(holder["TokenHolderQuantity"])
                all_holders.append((address, quantity_float))
                if len(all_holders) >= max_holders:
                    break

            if len(all_holders) >= max_holders:
                break

            if len(result_list) < offset:
                break

            page += 1
            time.sleep(0.5)

        # 2) Calculate total supply (int)
        total_supply = int(sum(quantity for _, quantity in all_holders))
        # 3) total number of holders
        total_holders = len(all_holders)

        # 4) Create CSV
        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["TokenHolderAddress", "TokenHolderQuantity"])
        for address, quantity_float in all_holders:
            quantity_str = str(int(quantity_float))
            writer.writerow([address, quantity_str])
        csv_buffer.seek(0)

        # 5) Summary text
        summary_text = (
            f"**Contract Address**: {contract_address}\n"
            f"**Total Holders**: {total_holders} (up to {max_holders})\n"
            f"**Total Supply**: {total_supply}\n\n"
            "Your CSV file is attached below.\n"
            "Note: Only up to 1000 holders are supported."
        )

        # 6) Log to Google Sheets
        user_name = str(interaction.user)
        worksheet.append_row(
            [user_name, contract_address, str(total_holders), str(total_supply)],
            value_input_option="RAW"
        )

        # 7) Reply with CSV file
        file_to_send = discord.File(fp=csv_buffer, filename="holderList.csv")
        await progress_message.edit(content="Snapshot completed! Sending file...")
        await interaction.followup.send(
            content=summary_text,
            ephemeral=True,
            file=file_to_send
        )

    @app_commands.command(
        name="register_wallet",
        description="Admin only: create an embed+button for users to register their wallet."
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        channel="Select the channel to post the embed and button"
    )
    async def register_wallet(self, interaction: discord.Interaction, channel: discord.TextChannel):
        """
        Admin command:
        Posts an embed (color #836EF9) and a button in the specified channel.
        """
        # Defer the response in ephemeral mode, so the command usage isn't visible
        await interaction.response.defer(ephemeral=True)

        # Embed の作成 (バーの色 #836EF9)
        embed = discord.Embed(
            title="Register your wallet",
            description="Click the button below to register your wallet.",
            color=0x836EF9
        )

        view = RegisterWalletView(sh)  # Viewにスプレッドシートの参照を渡す

        # チャンネルにメッセージを投稿 (全体に見える)
        await channel.send(embed=embed, view=view)

        # コマンド実行者にはエフェメラルで送信 (「投稿完了」など)
        await interaction.followup.send(
            content=f"Embed with a wallet registration button has been posted in {channel.mention}.",
            ephemeral=True
        )

# =========================
# ======== ここから追加（最適化版）========
# =========================

# 外部シート：wallet_log2 自動作成 & 並行アップサート
TARGET_SHEET_KEY = "14YADpxT8db3YjSPkmDLW971UqlAb-LXBLQPJqCliXhQ"

def _get_ws(spreadsheet, title, create=False):
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        if create:
            return spreadsheet.add_worksheet(title=title, rows=1000, cols=10)
        raise

def _upsert_by_discord_id(ws, user_name, user_id, wallet):
    """列: [DiscordName, DiscordID, WalletAddress] / DiscordIDで一意"""
    rows = ws.get_all_values()
    for idx, row in enumerate(rows, start=1):
        if len(row) >= 2 and row[1] == str(user_id):
            ws.update_cell(idx, 1, user_name)
            ws.update_cell(idx, 2, str(user_id))
            ws.update_cell(idx, 3, wallet)
            return "updated"
    ws.append_row([user_name, str(user_id), wallet], value_input_option="RAW")
    return "inserted"

def upsert_wallet_everywhere(local_spreadsheet, user_name, user_id, wallet):
    """ローカル wallet_log → upsert、外部 wallet_log / wallet_log2 → upsert（wallet_log2は自動生成）"""
    # ローカル
    ws_local = _get_ws(local_spreadsheet, "wallet_log", create=False)
    _upsert_by_discord_id(ws_local, user_name, user_id, wallet)

    # 外部
    ext = gc.open_by_key(TARGET_SHEET_KEY)
    ws1 = _get_ws(ext, "wallet_log", create=True)
    ws2 = _get_ws(ext, "wallet_log2", create=True)
    _upsert_by_discord_id(ws1, user_name, user_id, wallet)
    _upsert_by_discord_id(ws2, user_name, user_id, wallet)

def lookup_wallets(user_id):
    """ローカル/外部の現状ウォレットを確認（見つかった最初の値を返す）"""
    local = None
    try:
        ws_local = sh.worksheet("wallet_log")
        for row in ws_local.get_all_values():
            if len(row) >= 3 and row[1] == str(user_id):
                local = row[2]
                break
    except gspread.WorksheetNotFound:
        pass

    external = None
    try:
        ext = gc.open_by_key(TARGET_SHEET_KEY)
        for name in ["wallet_log", "wallet_log2"]:
            try:
                ws = ext.worksheet(name)
                for r in ws.get_all_values():
                    if len(r) >= 3 and r[1] == str(user_id):
                        external = r[2]
                        break
                if external:
                    break
            except gspread.WorksheetNotFound:
                continue
    except Exception:
        pass

    return local, external

# 1つの共通モーダル：登録/変更どちらにも利用（新ボタン専用。既存は触らない）
class RegisterOrChangeWalletModal(discord.ui.Modal):
    def __init__(self, spreadsheet, preset_wallet: str = ""):
        super().__init__(title="Register / Change your wallet")
        self.spreadsheet = spreadsheet
        self.wallet_input = discord.ui.TextInput(
            label="Wallet Address",
            placeholder=preset_wallet if preset_wallet else "Enter your wallet address",
            required=True,
            max_length=100
        )
        self.add_item(self.wallet_input)

    async def on_submit(self, interaction: discord.Interaction):
        user_name = str(interaction.user)
        user_id = str(interaction.user.id)
        wallet = self.wallet_input.value.strip()

        try:
            upsert_wallet_everywhere(self.spreadsheet, user_name, user_id, wallet)
        except gspread.WorksheetNotFound:
            await interaction.response.send_message(
                content="Local 'wallet_log' not found. Please create it first.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            content=f"✅ Wallet saved: **{wallet}**\n(Synced to local wallet_log & external wallet_log / wallet_log2)",
            ephemeral=True
        )

# 新しい2ボタン View（登録／確認変更）※既存のViewはそのまま
class WalletActionsView(discord.ui.View):
    def __init__(self, spreadsheet):
        super().__init__(timeout=None)
        self.spreadsheet = spreadsheet

    @discord.ui.button(label="Register your wallet", style=discord.ButtonStyle.primary)
    async def btn_register(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 空のプレースホルダでモーダルを開く（新規登録想定）
        await interaction.response.send_modal(RegisterOrChangeWalletModal(self.spreadsheet, preset_wallet=""))

    @discord.ui.button(label="Check / Change wallet", style=discord.ButtonStyle.secondary)
    async def btn_check_change(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 現在値を確認し、プレースホルダに入れてモーダルを表示（1回の応答枠をモーダルに使う）
        uid = str(interaction.user.id)
        local, external = lookup_wallets(uid)
        preset = local or external or ""
        await interaction.response.send_modal(RegisterOrChangeWalletModal(self.spreadsheet, preset_wallet=preset))

# 追加のコマンド群（新設）
class ExtraCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="post_wallet_buttons",
        description="Admin only: post an embed with Register and Check/Change wallet buttons."
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(channel="Select the channel to post the embed and buttons")
    async def post_wallet_buttons(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        embed = discord.Embed(
            title="Wallet actions",
            description="Register, check, or change your wallet.",
            color=0x836EF9
        )
        await channel.send(embed=embed, view=WalletActionsView(sh))
        await interaction.followup.send(
            content=f"✅ Posted wallet actions in {channel.mention}.",
            ephemeral=True
        )

    @app_commands.command(
        name="export_role_members",
        description="Admin only: export username & uid of members having the specified role (CSV, ephemeral)."
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(role="Select the role to export")
    async def export_role_members(self, interaction: discord.Interaction, role: discord.Role):
        """
        取得方法を最適化：
        - guild.chunk() でメンバーキャッシュを埋める（intents.members が必要）
        - role.members を直接CSV化
        """
        await interaction.response.defer(ephemeral=True)

        try:
            # メンバーキャッシュを埋める（必要に応じて分割ロード）
            await interaction.guild.chunk()

            members = list(role.members)
            csv_buf = io.StringIO()
            writer = csv.writer(csv_buf)
            writer.writerow(["UserName", "DiscordID"])
            for m in members:
                writer.writerow([m.name, str(m.id)])
            csv_buf.seek(0)

            file = discord.File(fp=csv_buf, filename=f"{role.name}_members.csv")
            await interaction.followup.send(
                content=f"Role **{role.name}** members: {len(members)} users.",
                ephemeral=True,
                file=file
            )
        except discord.Forbidden:
            await interaction.followup.send(
                content="Missing permissions/intent. Enable **Server Members Intent** in Developer Portal and re-invite if needed.",
                ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(content=f"Error: {e}", ephemeral=True)

# 既存のsetupに新Cogを足すのみ（既存は変更なし）
async def setup_bot():
    await bot.add_cog(SnapshotCog(bot))
    await bot.add_cog(ExtraCommands(bot))  # ★追加
    await bot.tree.sync()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")
    await bot.wait_until_ready()
    await setup_bot()

bot.run(DISCORD_TOKEN)
