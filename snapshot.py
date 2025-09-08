import os
import io
import csv
import json
import requests
import discord
import time
from datetime import datetime

from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# Google Sheets
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ========= ENV =========
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
API_KEY = os.getenv("API_KEY")
SERVICE_ACCOUNT_INFO = os.getenv("SERVICE_ACCOUNT_INFO")

# ========= Google Sheets Client =========
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_dict = json.loads(SERVICE_ACCOUNT_INFO)
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gc = gspread.authorize(creds)

# Local (existing)
sh = gc.open("snapshot_bot_log")
worksheet = sh.worksheet("log")  # existing snapshot logs

# ========= Discord Bot =========
intents = discord.Intents.default()
intents.members = True  # Required for role member export (Server Members Intent must be ON)
bot = commands.Bot(command_prefix="!", intents=intents)

# ========= Sheets helpers =========
WALLET_SHEET_MAP = {1: "wallet_log", 2: "wallet_log2", 3: "wallet_log3"}
ALL_WALLET_SHEETS = ["wallet_log", "wallet_log2", "wallet_log3"]

def _get_ws(spreadsheet, title, create=False):
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        if create:
            return spreadsheet.add_worksheet(title=title, rows=1000, cols=10)
        raise

def _find_row_by_id(ws, user_id: str):
    for idx, row in enumerate(ws.get_all_values(), start=1):
        if len(row) >= 2 and row[1] == user_id:
            return idx, row
    return None, None

def _lookup_wallet_in_sheet(ws, user_id: str):
    idx, row = _find_row_by_id(ws, user_id)
    if idx and len(row) >= 3:
        return row[0], row[2]  # (DiscordName, Wallet)
    return None, None

def _upsert_wallet(ws, user_name, user_id, wallet):
    idx, row = _find_row_by_id(ws, user_id)
    if idx:
        ws.update_cell(idx, 1, user_name)
        ws.update_cell(idx, 2, user_id)
        ws.update_cell(idx, 3, wallet)
    else:
        ws.append_row([user_name, user_id, wallet], value_input_option="RAW")

def find_wallet_any(user_id: str):
    """Search wallet across all wallet sheets, return (sheet_name, (name, wallet)) or (None, (None, None))."""
    for sheet_name in ALL_WALLET_SHEETS:
        try:
            ws = _get_ws(sh, sheet_name, create=True)
            name, wal = _lookup_wallet_in_sheet(ws, user_id)
            if wal:
                return sheet_name, (name, wal)
        except Exception:
            continue
    return None, (None, None)

def sync_wallet_to_sheet(sheet_name: str, user_name: str, user_id: str, wallet: str):
    ws = _get_ws(sh, sheet_name, create=True)
    _upsert_wallet(ws, user_name, user_id, wallet)

def sync_wallet_to_all(user_name: str, user_id: str, wallet: str):
    for s in ALL_WALLET_SHEETS:
        sync_wallet_to_sheet(s, user_name, user_id, wallet)

# ========= Bindings in snapshot_bot_log.bindings =========
def _get_bindings_ws():
    try:
        return sh.worksheet("bindings")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="bindings", rows=1000, cols=10)
        ws.append_row(["GuildID", "ChannelID", "MessageID", "SheetName", "CreatedAtISO"], value_input_option="RAW")
        return ws

def _is_sheet_already_bound(guild_id: int, sheet_name: str):
    ws = _get_bindings_ws()
    for row in ws.get_all_values()[1:]:
        if len(row) >= 4 and row[0] == str(guild_id) and row[3] == sheet_name:
            return True
    return False

def _add_binding(guild_id: int, channel_id: int, message_id: int, sheet_name: str):
    ws = _get_bindings_ws()
    ws.append_row([str(guild_id), str(channel_id), str(message_id), sheet_name, datetime.utcnow().isoformat()], value_input_option="RAW")

def _get_binding_by_message(message_id: int):
    ws = _get_bindings_ws()
    for row in ws.get_all_values()[1:]:
        if len(row) >= 3 and row[2] == str(message_id):
            return int(row[0]), int(row[1]), row[3]  # guild_id, channel_id, sheet_name
    return None

def _list_bindings_for_guild(guild_id: int):
    ws = _get_bindings_ws()
    out = []
    for row in ws.get_all_values()[1:]:
        if len(row) >= 5 and row[0] == str(guild_id):
            out.append({"guild_id": int(row[0]), "channel_id": int(row[1]), "message_id": int(row[2]),
                        "sheet_name": row[3], "created_at": row[4]})
    return out

# ========= Existing snapshot (unchanged) =========
class SnapshotCog(commands.Cog):
    def __init__(self, bot): self.bot = bot

    @app_commands.command(name="snapshot", description="Fetch token holder info for a contract address (ephemeral).")
    @app_commands.describe(contract_address="Enter the token contract address")
    async def snapshot(self, interaction: discord.Interaction, contract_address: str):
        await interaction.response.defer(ephemeral=True)
        progress_message = await interaction.followup.send(content="Fetching token holders... (page 1)", ephemeral=True)
        base_url = "https://api.socialscan.io/monad-testnet/v1/developer/api"
        module, action, offset, page = "token", "tokenholderlist", 100, 1
        all_holders, max_consecutive_errors, error_count, max_holders = [], 5, 0, 1000
        while True:
            await progress_message.edit(content=f"Now reading page {page}...")
            params = {"module": module, "action": action, "contractaddress": contract_address,
                      "page": page, "offset": offset, "apikey": API_KEY}
            response = requests.get(base_url, params=params)
            if response.status_code != 200:
                error_count += 1
                if error_count >= max_consecutive_errors: break
                time.sleep(0.5); continue
            data = response.json()
            if data.get("status") != "1":
                error_count += 1
                if error_count >= max_consecutive_errors: break
                time.sleep(0.5); continue
            else:
                error_count = 0
            result_list = data.get("result")
            if not result_list: break
            for holder in result_list:
                all_holders.append((holder["TokenHolderAddress"], float(holder["TokenHolderQuantity"])))
                if len(all_holders) >= max_holders: break
            if len(all_holders) >= max_holders or len(result_list) < offset: break
            page += 1; time.sleep(0.5)

        total_supply = int(sum(q for _, q in all_holders))
        total_holders = len(all_holders)
        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["TokenHolderAddress", "TokenHolderQuantity"])
        for address, q in all_holders:
            writer.writerow([address, str(int(q))])
        csv_buffer.seek(0)

        summary = (f"**Contract Address**: {contract_address}\n"
                   f"**Total Holders**: {total_holders} (up to {max_holders})\n"
                   f"**Total Supply**: {total_supply}\n\nYour CSV file is attached below.")
        worksheet.append_row([str(interaction.user), contract_address, str(total_holders), str(total_supply)],
                             value_input_option="RAW")
        await progress_message.edit(content="Snapshot completed! Sending file...")
        await interaction.followup.send(content=summary, ephemeral=True,
                                        file=discord.File(fp=io.StringIO(csv_buffer.getvalue()), filename="holderList.csv"))

# ========= Role export (union, dedup) =========
class RoleExport(commands.Cog):
    def __init__(self, bot): self.bot = bot

    @app_commands.command(
        name="export_role_members",
        description="Admin only: export username & uid of members having the specified role(s) (CSV, ephemeral)."
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(role="Primary role", role2="(Optional) Additional role", role3="(Optional) Additional role")
    async def export_role_members(self, interaction: discord.Interaction, role: discord.Role,
                                  role2: discord.Role | None = None, role3: discord.Role | None = None):
        await interaction.response.defer(ephemeral=True)
        try:
            await interaction.guild.chunk()
            roles = [r for r in [role, role2, role3] if r]
            matched_map, member_set = {}, set()
            for r in roles:
                for m in r.members:
                    member_set.add(m); matched_map.setdefault(m.id, set()).add(r.name)
            buf = io.StringIO(); w = csv.writer(buf); w.writerow(["UserName", "DiscordID", "RolesMatched"])
            for m in sorted(member_set, key=lambda x: (x.name, x.id)):
                w.writerow([m.name, str(m.id), ",".join(sorted(matched_map.get(m.id, [])))])
            buf.seek(0)
            file = discord.File(fp=io.StringIO(buf.getvalue()), filename=f"members_{'-'.join([r.name for r in roles])}.csv")
            await interaction.followup.send(content=f"Exported **{len(member_set)}** members.", ephemeral=True, file=file)
        except discord.Forbidden:
            await interaction.followup.send(content="Missing **Server Members Intent**.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(content=f"Error: {e}", ephemeral=True)

# ========= Wallet Hub (single command) =========
def _sheet_from_button_number(n: int) -> str:
    if n not in WALLET_SHEET_MAP: raise ValueError("button_number must be 1, 2 or 3")
    return WALLET_SHEET_MAP[n]

class ConfirmChangeView(discord.ui.View):
    def __init__(self, sheet_name: str, current_wallet: str, user_name: str):
        super().__init__(timeout=60)
        self.sheet_name = sheet_name
        self.current_wallet = current_wallet
        self.user_name = user_name

    @discord.ui.button(label="Confirm change", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RegisterOrChangeWalletModal(self.sheet_name, preset_wallet=self.current_wallet, is_change=True, user_name=self.user_name))

class RegisterOrChangeWalletModal(discord.ui.Modal):
    def __init__(self, sheet_name: str, preset_wallet: str = "", is_change: bool = False, user_name: str = ""):
        super().__init__(title="Change your wallet" if is_change else "Register your wallet")
        self.sheet_name = sheet_name
        self.is_change = is_change
        self.user_name_override = user_name
        self.wallet_input = discord.ui.TextInput(
            label="Wallet Address",
            placeholder=preset_wallet if preset_wallet else "Enter your wallet address",
            required=True, max_length=100
        )
        self.add_item(self.wallet_input)

    async def on_submit(self, interaction: discord.Interaction):
        user_name = self.user_name_override or str(interaction.user)
        user_id = str(interaction.user.id)
        new_wallet = self.wallet_input.value.strip()

        # Change: update ALL sheets to keep single wallet per user
        if self.is_change:
            sync_wallet_to_all(user_name, user_id, new_wallet)
            await interaction.response.send_message(
                content=f"✅ Wallet changed to **{new_wallet}**\n**User**: {user_name} (synced across all lists)",
                ephemeral=True
            )
        else:
            # Register: if user already has wallet somewhere else, we already auto-synced before opening modal.
            # Here, we accept first-time input and then sync to ALL sheets.
            sync_wallet_to_all(user_name, user_id, new_wallet)
            await interaction.response.send_message(
                content=f"✅ Registration completed.\n**User**: {user_name}\n**Wallet**: {new_wallet} (synced across all lists)",
                ephemeral=True
            )

class WalletHubView(discord.ui.View):
    """3 buttons bound to a specific sheet; auto-sync & single-wallet guarantee."""
    def __init__(self):
        super().__init__(timeout=None)

    def _bound_sheet(self, interaction: discord.Interaction) -> str:
        binding = _get_binding_by_message(interaction.message.id)
        if not binding: raise RuntimeError("No binding for this message.")
        return binding[2]

    async def _autosync_if_needed(self, interaction: discord.Interaction, sheet_name: str, user_name: str, user_id: str):
        """On ANY button: if wallet exists in any sheet but not in bound sheet, copy it to bound sheet."""
        src_sheet, (name, wal) = find_wallet_any(user_id)
        if wal:
            # copy to bound sheet (if missing or different)
            sync_wallet_to_sheet(sheet_name, user_name, user_id, wal)
            return name or user_name, wal, True  # (name, wallet, was_synced)
        return None, None, False

    @discord.ui.button(label="Register wallet", style=discord.ButtonStyle.primary, row=0)
    async def btn_register(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            sheet = self._bound_sheet(interaction)
            user_name, user_id = str(interaction.user), str(interaction.user.id)

            # Global wallet exists? autosync and just inform.
            name, wal, synced = await self._autosync_if_needed(interaction, sheet, user_name, user_id)
            if synced:
                await interaction.response.send_message(
                    content=f"📝 Already submitted elsewhere. Synced here.\n**User**: {name}\n**Wallet**: {wal}",
                    ephemeral=True
                )
                return

            # No global wallet: open register modal
            await interaction.response.send_modal(RegisterOrChangeWalletModal(sheet, preset_wallet="", is_change=False, user_name=user_name))
        except Exception as e:
            await interaction.response.send_message(content=f"Configuration error: {e}", ephemeral=True)

    @discord.ui.button(label="Check wallet", style=discord.ButtonStyle.success, row=0)
    async def btn_check(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            sheet = self._bound_sheet(interaction)
            user_name, user_id = str(interaction.user), str(interaction.user.id)

            # autosync from any other sheet if exists
            name, wal, _ = await self._autosync_if_needed(interaction, sheet, user_name, user_id)

            ws = _get_ws(sh, sheet, create=True)
            shown_name, shown_wallet = _lookup_wallet_in_sheet(ws, user_id)

            if shown_wallet:
                await interaction.response.send_message(
                    content=f"**User**: {shown_name}\n**Wallet**: {shown_wallet}",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    content="No wallet found for your account. Please register first.",
                    ephemeral=True
                )
        except Exception as e:
            await interaction.response.send_message(content=f"Configuration error: {e}", ephemeral=True)

    @discord.ui.button(label="Change wallet", style=discord.ButtonStyle.danger, row=0)
    async def btn_change(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            sheet = self._bound_sheet(interaction)
            user_name, user_id = str(interaction.user), str(interaction.user.id)

            # If wallet exists anywhere, ensure it is present here before change
            name, wal, _ = await self._autosync_if_needed(interaction, sheet, user_name, user_id)

            # Get current (prefer bound sheet; fallback to global)
            ws = _get_ws(sh, sheet, create=True)
            shown_name, shown_wallet = _lookup_wallet_in_sheet(ws, user_id)
            if not shown_wallet:
                _, (gname, gwal) = find_wallet_any(user_id)
                shown_wallet = gwal; shown_name = gname or user_name

            if not shown_wallet:
                await interaction.response.send_message(
                    content="No wallet found for your account. Please register first.",
                    ephemeral=True
                )
                return

            msg = f"Current wallet: **{shown_wallet}**\nProceed to change?"
            await interaction.response.send_message(content=msg, ephemeral=True,
                                                    view=ConfirmChangeView(sheet, current_wallet=shown_wallet, user_name=shown_name))
        except Exception as e:
            await interaction.response.send_message(content=f"Configuration error: {e}", ephemeral=True)

class WalletHub(commands.Cog):
    def __init__(self, bot): self.bot = bot

    @app_commands.command(
        name="register_wallet",
        description="Admin only: Post a wallet hub with 3 buttons bound to a specific sheet (1=wallet_log, 2=wallet_log2, 3=wallet_log3)."
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(channel="Target channel", button_number="1=wallet_log, 2=wallet_log2, 3=wallet_log3")
    async def register_wallet(self, interaction: discord.Interaction, channel: discord.TextChannel,
                              button_number: app_commands.Range[int, 1, 3]):
        await interaction.response.defer(ephemeral=True)

        sheet_name = _sheet_from_button_number(button_number)
        if _is_sheet_already_bound(interaction.guild_id, sheet_name):
            await interaction.followup.send(content=f"❌ Binding already exists for **{sheet_name}**.", ephemeral=True)
            return

        # ensure sheet exists
        _get_ws(sh, sheet_name, create=True)

        # balanced embed (image optional)
        embed = discord.Embed(
            title="Wallet Center",
            description="Register, check, or change your wallet.\nAll actions are ephemeral (visible only to you).",
            color=0x836EF9
        )
        embed.set_footer(text="Secure • Fast • Private")
        img_path = "./C_logo.png"
        file = discord.File(img_path, filename="C_logo.png") if os.path.exists(img_path) else None
        if file: embed.set_thumbnail(url="attachment://C_logo.png")

        view = WalletHubView()
        msg = await (channel.send(embed=embed, view=view, file=file) if file else channel.send(embed=embed, view=view))

        _add_binding(interaction.guild_id, channel.id, msg.id, sheet_name)
        await interaction.followup.send(content=f"✅ Posted wallet hub in {channel.mention} (bound to **{sheet_name}**).", ephemeral=True)

# ========= Admin diagnostics =========
class AdminDiagnostics(commands.Cog):
    def __init__(self, bot): self.bot = bot

    @app_commands.command(
        name="check_sheet_binding",
        description="Admin only: show bound wallet sheets and their channel/message IDs in this server."
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def check_sheet_binding(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        bindings = _list_bindings_for_guild(interaction.guild_id)
        if not bindings:
            await interaction.followup.send(content="No bindings found in this server.", ephemeral=True); return
        embed = discord.Embed(title="Current Wallet Button Bindings",
                              description="Active bindings for this server.",
                              color=0x4BB543)
        for b in bindings:
            embed.add_field(
                name=b["sheet_name"],
                value=f"Channel: <#{b['channel_id']}>\nChannelID: `{b['channel_id']}`\nMessageID: `{b['message_id']}`\nCreated: `{b['created_at']}`",
                inline=False
            )
        embed.set_footer(text="Use /register_wallet (admin) to add more bindings.")
        await interaction.followup.send(embed=embed, ephemeral=True)

# ========= Setup & Run =========
async def setup_bot():
    await bot.add_cog(SnapshotCog(bot))     # existing
    await bot.add_cog(RoleExport(bot))      # role export
    await bot.add_cog(WalletHub(bot))       # unified wallet hub
    await bot.add_cog(AdminDiagnostics(bot))# diagnostics
    await bot.tree.sync()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")
    await bot.wait_until_ready()
    await setup_bot()

bot.run(DISCORD_TOKEN)
