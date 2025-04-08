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
