##############################
# 1) importを先頭に集約
##############################
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


##############################
# 2) 既存コード
##############################

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
        self.spreadsheet = spreadsheet

        self.wallet_input = discord.ui.TextInput(
            label="Wallet Address",
            placeholder="Enter your wallet address here",
            required=True,
            max_length=100
        )
        self.add_item(self.wallet_input)

    async def on_submit(self, interaction: discord.Interaction):
        user_name = str(interaction.user)
        user_id = str(interaction.user.id)
        wallet_address = self.wallet_input.value.strip()

        try:
            register_worksheet = self.spreadsheet.worksheet("wallet_log")
        except gspread.WorksheetNotFound:
            await interaction.response.send_message(
                content="The sheet 'wallet_log' was not found. Please create it first.",
                ephemeral=True
            )
            return

        all_values = register_worksheet.get_all_values()
        existing_row = None
        for row in all_values:
            # row = [DiscordName, DiscordID, WalletAddress]
            if len(row) >= 2 and row[1] == user_id:
                existing_row = row
                break

        if existing_row:
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
    """
    def __init__(self, spreadsheet):
        super().__init__(timeout=None)
        self.spreadsheet = spreadsheet

    @discord.ui.button(label="Register your wallet", style=discord.ButtonStyle.primary)
    async def register_wallet_button(self, interaction: discord.Interaction, button: discord.ui.Button):
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
        await interaction.response.defer(ephemeral=True)
        progress_message = await interaction.followup.send(
            content="Fetching token holders... (page 1)",
            ephemeral=True
        )

        base_url = "https://api.socialscan.io/monad-testnet/v1/developer/api"
        module = "token"
        action = "tokenholderlist"
        offset = 100
        page = 1

        all_holders = []
        max_consecutive_errors = 5
        error_count = 0
        max_holders = 1000

        while True:
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

        total_supply = int(sum(quantity for _, quantity in all_holders))
        total_holders = len(all_holders)

        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["TokenHolderAddress", "TokenHolderQuantity"])
        for address, quantity_float in all_holders:
            quantity_str = str(int(quantity_float))
            writer.writerow([address, quantity_str])
        csv_buffer.seek(0)

        summary_text = (
            f"**Contract Address**: {contract_address}\n"
            f"**Total Holders**: {total_holders} (up to {max_holders})\n"
            f"**Total Supply**: {total_supply}\n\n"
            "Your CSV file is attached below.\n"
            "Note: Only up to 1000 holders are supported."
        )

        user_name = str(interaction.user)
        worksheet.append_row(
            [user_name, contract_address, str(total_holders), str(total_supply)],
            value_input_option="RAW"
        )

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
    @app_commands.describe(channel="Select the channel to post the embed and button")
    async def register_wallet(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        embed = discord.Embed(
            title="Register your wallet",
            description="Click the button below to register your wallet.",
            color=0x836EF9
        )

        view = RegisterWalletView(sh)
        await channel.send(embed=embed, view=view)
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

"""
bot.run(DISCORD_TOKEN)
import time
import gspread
import discord
from discord import app_commands
from discord.ext import commands
"""

##############################
# 3) Collab.Mon追加パート
##############################

def get_or_create_worksheet(spreadsheet, sheet_name: str):
    try:
        ws = spreadsheet.worksheet(sheet_name)
        return ws
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=sheet_name, rows=100, cols=20)

class CollabMonWalletsView(discord.ui.View):
    def __init__(self, spreadsheet, user: discord.User):
        super().__init__(timeout=None)
        self.spreadsheet = spreadsheet
        self.user = user

        # collabmon_wallet_log シートを確保
        self.wallet_sheet = get_or_create_worksheet(self.spreadsheet, "collabmon_wallet_log")

        # 該当ユーザーのウォレット一覧を取得 ([UserID, WalletAddress, Timestamp]想定)
        all_values = self.wallet_sheet.get_all_values()
        matched = [row for row in all_values if len(row) >= 2 and row[0] == str(user.id)]
        self.user_wallets = []
        for row in matched:
            if len(row) > 1:
                self.user_wallets.append(row[1])

        # リンクボタンはコールバックと併用不可。
        # → 直接 Button オブジェクトを生成し、urlを指定して add_item する
        add_wallet_button = discord.ui.Button(
            label="Add a new wallet",
            style=discord.ButtonStyle.link,
            url="https://example.com"
        )
        self.add_item(add_wallet_button)

    @discord.ui.button(label="Use connected wallets", style=discord.ButtonStyle.primary)
    async def use_connected_wallets(self, interaction: discord.Interaction, button: discord.ui.Button):
        print("[DEBUG] 'Use connected wallets' button clicked.")
        if not self.user_wallets:
            await interaction.response.send_message(
                content="No connected wallets found. Please add a new wallet.",
                ephemeral=True
            )
            return

        wallet_list_text = "\n".join(self.user_wallets)
        await interaction.response.send_message(
            content=(
                f"Using your connected wallet(s):\n```\n{wallet_list_text}\n```\n"
                "(Here we would verify your NFT...)"
            ),
            ephemeral=True
        )

class CollabMonView(discord.ui.View):
    def __init__(self, spreadsheet):
        super().__init__(timeout=None)
        self.spreadsheet = spreadsheet

    @discord.ui.button(label="Let's go!", style=discord.ButtonStyle.primary)
    async def lets_go(self, interaction: discord.Interaction, button: discord.ui.Button):
        print("[DEBUG] 'Let's go!' button clicked.")
        user_wallets_view = CollabMonWalletsView(self.spreadsheet, interaction.user)

        if user_wallets_view.user_wallets:
            wallet_list_text = "\n".join(user_wallets_view.user_wallets)
            text = (
                "My Connected Wallets\n"
                "Collab.Mon now supports wallet verification.\n\n"
                f"evm:\n```\n{wallet_list_text}\n```"
            )
        else:
            text = "You have no connected wallets.\nPlease add a new wallet."

        await interaction.response.send_message(
            content=text,
            view=user_wallets_view,
            ephemeral=True
        )

    @discord.ui.button(label="docs", style=discord.ButtonStyle.secondary)
    async def docs(self, interaction: discord.Interaction, button: discord.ui.Button):
        print("[DEBUG] 'docs' button clicked.")
        await interaction.response.send_message(
            content="（docsボタン押下のテスト）後ほど実際の文書やリンクに差し替えてください。",
            ephemeral=True
        )

class SetupVerifyCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="setupverify",
        description="Admin only: sets up the Collab.Mon verification embed with two buttons."
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        channel="Select the channel to post verification embed",
        contract_address="Enter the contract address for verification"
    )
    async def setupverify(self, interaction: discord.Interaction, channel: discord.TextChannel, contract_address: str):
        print("[DEBUG] /setupverify invoked.")
        server_config_ws = get_or_create_worksheet(sh, "server_config")

        guild_id = interaction.guild.id if interaction.guild else "N/A"
        guild_name = interaction.guild.name if interaction.guild else "N/A"
        channel_id = channel.id
        now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        row_data = [str(guild_id), guild_name, str(channel_id), contract_address, now_str]
        server_config_ws.append_row(row_data, value_input_option="RAW")

        embed = discord.Embed(
            title="Collab.Mon",
            description=(
                "Verify your assets\n"
                "This is a read-only connection. Do not share your private keys. "
                "We will never ask for your seed phrase. We will never DM you."
            ),
            color=0x836EF9
        )
        view = CollabMonView(sh)

        await channel.send(embed=embed, view=view)

        await interaction.response.send_message(
            content=(
                f"Collab.Mon Verify embed has been posted to {channel.mention}.\n"
                f"Contract: `{contract_address}` recorded."
            ),
            ephemeral=True
        )

@bot.listen("on_ready")
async def add_setupverify_cog():
    print("[DEBUG] on_ready -> add_setupverify_cog() triggered.")
    if bot.get_cog("SetupVerifyCog") is None:
        await bot.add_cog(SetupVerifyCog(bot))
        await bot.tree.sync()
        print("SetupVerifyCog loaded and slash commands synced.")

##############################
# ファイル末尾の起動
##############################
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
