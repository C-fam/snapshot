import os
import io
import csv
import json
import requests
import discord
import time

from discord import app_commands
from discord.ext import commands
from discord.ui import View, Button, Modal, TextInput
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
worksheet = sh.worksheet("log")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ------------------------------
# モーダル (ウォレットアドレス入力フォーム)
# ------------------------------
class RegisterWalletModal(Modal, title="Register Wallet"):
    wallet = TextInput(
        label="Your wallet address",
        placeholder="Enter your wallet address here...",
        required=True,
        max_length=100
    )

    def __init__(self, sh_reference, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # GoogleSheetsへの参照を受け取って使い回す
        self.sh = sh_reference

    async def on_submit(self, interaction: discord.Interaction):
        """ユーザーが[Submit]を押した時の処理"""
        user_name = str(interaction.user)
        user_id = str(interaction.user.id)
        wallet_address = self.wallet.value.strip()

        try:
            # "wallet_log" シートが存在するか確認
            try:
                register_worksheet = self.sh.worksheet("wallet_log")
            except gspread.WorksheetNotFound:
                # なければメッセージを返して終了
                await interaction.response.send_message(
                    content="The sheet 'wallet_log' was not found. Please create it first.",
                    ephemeral=True
                )
                return

            # シートから全データを取得し、既に同じuser_idがあるか確認
            all_values = register_worksheet.get_all_values()
            existing_row = None
            for row in all_values:
                # row = [DiscordName, DiscordID, WalletAddress]
                if len(row) >= 2 and row[1] == user_id:
                    existing_row = row
                    break

            if existing_row:
                # 登録済み
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
                # 未登録 -> 新規登録
                register_worksheet.append_row([user_name, user_id, wallet_address], value_input_option="RAW")
                await interaction.response.send_message(
                    content=(
                        f"Your wallet has been registered.\n"
                        f"**Name**: {user_name}\n"
                        f"**Wallet**: {wallet_address}"
                    ),
                    ephemeral=True
                )

        except Exception as e:
            # 何らかのエラーがあれば安全に終了し、エラーメッセージを表示
            print(e)  # デバッグ用にログ
            await interaction.response.send_message(
                content="An unexpected error occurred while accessing Google Sheets. Please try again later.",
                ephemeral=True
            )

# ------------------------------
# ビュー (ボタン配置)
# ------------------------------
class RegisterWalletView(View):
    """ユーザーが押せるボタンを含むビュー"""
    def __init__(self, sh_reference, timeout=180):
        super().__init__(timeout=timeout)
        self.sh = sh_reference  # スプレッドシートへの参照を保持

    @discord.ui.button(label="Register Wallet", style=discord.ButtonStyle.primary)
    async def register_wallet_button(self, interaction: discord.Interaction, button: Button):
        """「Register Wallet」ボタンを押した時にモーダルを表示"""
        modal = RegisterWalletModal(sh_reference=self.sh)
        await interaction.response.send_modal(modal)

# ------------------------------
# コグ
# ------------------------------
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

        # 1) Collect all holder info (including duplicates) in a list
        all_holders = []

        # Stop if too many consecutive errors occur
        max_consecutive_errors = 5
        error_count = 0

        # Limit to 1000 holders
        max_holders = 1000

        while True:
            # Update progress message
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

            # If an error occurs, don't stop immediately
            if response.status_code != 200:
                error_count += 1
                if error_count >= max_consecutive_errors:
                    break
                time.sleep(0.5)
                continue  # retry the same page

            data = response.json()

            # If API status is not "1" or no results returned
            if data.get("status") != "1":
                error_count += 1
                if error_count >= max_consecutive_errors:
                    break
                time.sleep(0.5)
                continue  # retry the same page
            else:
                # Reset error counter on success
                error_count = 0

            result_list = data.get("result")
            if not result_list:
                # No data -> end
                break

            for holder in result_list:
                address = holder["TokenHolderAddress"]
                quantity_float = float(holder["TokenHolderQuantity"])
                all_holders.append((address, quantity_float))

                # Stop at 1000
                if len(all_holders) >= max_holders:
                    break

            if len(all_holders) >= max_holders:
                break

            # If fewer results than offset, we've reached the final page
            if len(result_list) < offset:
                break

            page += 1
            time.sleep(0.5)

        # 2) Calculate total supply as an integer
        total_supply = int(sum(quantity for _, quantity in all_holders))

        # 3) Total number of holders (including duplicates)
        total_holders = len(all_holders)

        # 4) Create CSV
        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["TokenHolderAddress", "TokenHolderQuantity"])

        for address, quantity_float in all_holders:
            quantity_str = str(int(quantity_float))  # truncate decimals
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
        name="registerwallet",
        description="(Admin only) Create a wallet-registration button for users."
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def registerwallet(self, interaction: discord.Interaction):
        """
        Admin-only command: posts a public message with a button to open a wallet registration modal.
        """
        # ここではエフェメラル = False で投稿し、全員がボタンを押せるようにする
        view = RegisterWalletView(sh_reference=sh)
        await interaction.response.send_message(
            content="Click the button below to register your wallet.",
            view=view,
            ephemeral=False
        )

async def setup_bot():
    await bot.add_cog(SnapshotCog(bot))
    await bot.tree.sync()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")
    await bot.wait_until_ready()
    await setup_bot()

bot.run(DISCORD_TOKEN)
