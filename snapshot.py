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

# =========================
# Load environment variables
# =========================
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
API_KEY = os.getenv("API_KEY")
SERVICE_ACCOUNT_INFO = os.getenv("SERVICE_ACCOUNT_INFO")

# =========================
# Google Sheets client
# =========================
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_dict = json.loads(SERVICE_ACCOUNT_INFO)
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gc = gspread.authorize(creds)

# Local log spreadsheet (existing)
sh = gc.open("snapshot_bot_log")
worksheet = sh.worksheet("log")  # For snapshot logs

# External spreadsheet (for parallel wallet sync; do not expose tab names)
TARGET_SHEET_KEY = "14YADpxT8db3YjSPkmDLW971UqlAb-LXBLQPJqCliXhQ"

# =========================
# Discord Bot
# =========================
intents = discord.Intents.default()
intents.members = True  # Required for role member export (Server Members Intent must be ON)
bot = commands.Bot(command_prefix="!", intents=intents)

# -------- (Existing) Modal: Register wallet -------- #
class RegisterWalletModal(discord.ui.Modal):
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

# -------- (Existing) View: Register button -------- #
class RegisterWalletView(discord.ui.View):
    def __init__(self, spreadsheet):
        super().__init__(timeout=None)
        self.spreadsheet = spreadsheet

    @discord.ui.button(label="Register your wallet", style=discord.ButtonStyle.primary)
    async def register_wallet_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = RegisterWalletModal(self.spreadsheet)
        await interaction.response.send_modal(modal)

# -------- (Existing) Cog -------- #
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
                error_count = 0

            result_list = data.get("result")
            if not result_list:
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

        file_to_send = discord.File(fp=io.StringIO(csv_buffer.getvalue()), filename="holderList.csv")
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
        # (kept as-is; you will stop using this to avoid duplication)
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

# =========================
# ======== Additions ======
# =========================

def _get_ws(spreadsheet, title, create=False):
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        if create:
            return spreadsheet.add_worksheet(title=title, rows=1000, cols=10)
        raise

def _upsert_by_discord_id(ws, user_name, user_id, wallet):
    # columns: [DiscordName, DiscordID, WalletAddress]
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
    # local
    ws_local = _get_ws(local_spreadsheet, "wallet_log", create=False)
    _upsert_by_discord_id(ws_local, user_name, user_id, wallet)
    # external (do not expose tabs publicly; admin-only diagnostics will not list tab names)
    ext = gc.open_by_key(TARGET_SHEET_KEY)
    _upsert_by_discord_id(_get_ws(ext, "wallet_log", create=True), user_name, user_id, wallet)
    _upsert_by_discord_id(_get_ws(ext, "wallet_log2", create=True), user_name, user_id, wallet)

def lookup_current_wallet(user_id):
    local = None
    try:
        ws_local = sh.worksheet("wallet_log")
        for row in ws_local.get_all_values():
            if len(row) >= 3 and row[1] == str(user_id):
                local = row[2]; break
    except gspread.WorksheetNotFound:
        pass

    # external check but do not reveal which tab in public replies
    external = None
    try:
        ext = gc.open_by_key(TARGET_SHEET_KEY)
        for name in ["wallet_log", "wallet_log2"]:
            try:
                ws = ext.worksheet(name)
                for r in ws.get_all_values():
                    if len(r) >= 3 and r[1] == str(user_id):
                        external = r[2]; break
                if external: break
            except gspread.WorksheetNotFound:
                continue
    except Exception:
        pass
    return local or external

# Unified modal for new buttons (English UI; no sheet names exposed)
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
                content="Configuration error: local sheet is missing. Please contact an admin.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            content=f"✅ Wallet saved: **{wallet}**",
            ephemeral=True
        )

# New, balanced 2-button view with thumbnail image
class WalletActionsView(discord.ui.View):
    def __init__(self, spreadsheet):
        super().__init__(timeout=None)
        self.spreadsheet = spreadsheet

    @discord.ui.button(label="Register wallet", style=discord.ButtonStyle.primary, row=0)
    async def btn_register(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RegisterOrChangeWalletModal(self.spreadsheet, preset_wallet=""))

    @discord.ui.button(label="Check / Change wallet", style=discord.ButtonStyle.secondary, row=0)
    async def btn_check_change(self, interaction: discord.Interaction, button: discord.ui.Button):
        preset = lookup_current_wallet(str(interaction.user.id)) or ""
        await interaction.response.send_modal(RegisterOrChangeWalletModal(self.spreadsheet, preset_wallet=preset))

class ExtraCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ---- New: Post new embed with balanced layout (English UI) ----
    @app_commands.command(
        name="post_wallet_buttons",
        description="Admin only: post a clean embed with 'Register' and 'Check/Change' buttons (English UI)."
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(channel="Channel to post the embed into")
    async def post_wallet_buttons(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)

        # Build embed
        embed = discord.Embed(
            title="Wallet Center",
            description=(
                "Register your wallet, or check & change it any time.\n"
                "Your submission is private (ephemeral)."
            ),
            color=0x836EF9
        )
        embed.set_footer(text="Secure • Fast • Private")

        file = None
        image_path = "./C_logo.png"  # place your provided image as C_logo.png next to this script
        if os.path.exists(image_path):
            file = discord.File(image_path, filename="C_logo.png")
            embed.set_thumbnail(url="attachment://C_logo.png")

        view = WalletActionsView(sh)
        if file:
            await channel.send(embed=embed, view=view, file=file)
        else:
            await channel.send(embed=embed, view=view)

        await interaction.followup.send(
            content=f"✅ Posted wallet actions in {channel.mention}.",
            ephemeral=True
        )

    # ---- Keep: Role export (unchanged) ----
    @app_commands.command(
        name="export_role_members",
        description="Admin only: export username & uid of members having the specified role (CSV, ephemeral)."
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(role="Select the role to export")
    async def export_role_members(self, interaction: discord.Interaction, role: discord.Role):
        await interaction.response.defer(ephemeral=True)
        try:
            await interaction.guild.chunk()
            members = list(role.members)
            csv_buf = io.StringIO()
            writer = csv.writer(csv_buf)
            writer.writerow(["UserName", "DiscordID"])
            for m in members:
                writer.writerow([m.name, str(m.id)])
            csv_buf.seek(0)
            file = discord.File(fp=io.StringIO(csv_buf.getvalue()), filename=f"{role.name}_members.csv")
            await interaction.followup.send(
                content=f"Role **{role.name}** members: {len(members)} users.",
                ephemeral=True,
                file=file
            )
        except discord.Forbidden:
            await interaction.followup.send(
                content="Missing permissions/intent. Enable **Server Members Intent** in Developer Portal.",
                ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(content=f"Error: {e}", ephemeral=True)

    # ---- New: Admin-only sheet binding check (no tab names exposed) ----
    @app_commands.command(
        name="check_sheet_binding",
        description="Admin only: check which spreadsheets this bot is bound to (no sheet/tab names exposed)."
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def check_sheet_binding(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            local_title = sh.title
        except Exception:
            local_title = "(unavailable)"

        try:
            ext = gc.open_by_key(TARGET_SHEET_KEY)
            external_title = ext.title
        except Exception:
            external_title = "(unavailable)"

        # Mask the key for safety
        key = TARGET_SHEET_KEY
        masked_key = key[:6] + "..." + key[-6:] if len(key) > 12 else key

        embed = discord.Embed(
            title="Sheet Binding (Admin)",
            description="Current spreadsheet linkage status.",
            color=0x4BB543
        )
        embed.add_field(name="Local Spreadsheet", value=f"**{local_title}**", inline=False)
        embed.add_field(name="External Spreadsheet", value=f"**{external_title}**\nKey: `{masked_key}`", inline=False)
        embed.set_footer(text="Tabs are intentionally not displayed.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---- New: Purge old wallet button messages (to avoid duplication) ----
    @app_commands.command(
        name="purge_wallet_buttons",
        description="Admin only: remove previous wallet button messages posted by this bot in a channel."
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(channel="Channel to purge from", limit="Max messages to scan (default 200)")
    async def purge_wallet_buttons(self, interaction: discord.Interaction, channel: discord.TextChannel, limit: int = 200):
        await interaction.response.defer(ephemeral=True)

        deleted = 0
        try:
            async for msg in channel.history(limit=limit):
                if msg.author.id != bot.user.id:
                    continue
                # Heuristics: messages with our known titles OR with components (buttons)
                titles = {"Register your wallet", "Wallet Center", "Wallet actions"}
                has_target_embed = any((e.title in titles) for e in msg.embeds) if msg.embeds else False
                has_components = bool(msg.components)

                if has_target_embed or has_components:
                    try:
                        await msg.delete()
                        deleted += 1
                    except discord.Forbidden:
                        continue
                    except discord.HTTPException:
                        continue
        except Exception as e:
            await interaction.followup.send(content=f"Error while purging: {e}", ephemeral=True)
            return

        await interaction.followup.send(content=f"✅ Purged {deleted} message(s).", ephemeral=True)

# =========================
# Setup & Run
# =========================
async def setup_bot():
    await bot.add_cog(SnapshotCog(bot))    # existing
    await bot.add_cog(ExtraCommands(bot))  # additions
    await bot.tree.sync()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")
    await bot.wait_until_ready()
    await setup_bot()

bot.run(DISCORD_TOKEN)
