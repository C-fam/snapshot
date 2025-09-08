import os
import io
import csv
import json
import time
import requests
import discord
from datetime import datetime
from typing import Tuple, Optional, Dict, Any, Callable

from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# Google Sheets
import gspread
from gspread.exceptions import APIError, WorksheetNotFound
from oauth2client.service_account import ServiceAccountCredentials

# ========= CONFIG =========
# Check / Change ボタンでも、マスターにウォレットがあれば現在のシートへ登録するか？
AUTO_ENROLL_FROM_MASTER_ON_ANY_BUTTON = True  # True にすると「どのボタンでも登録」可

# Embed image
EMBED_IMAGE_PATH = "./C_logo.png"

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

# ========= Friendly error helper =========
async def send_friendly_error(interaction: discord.Interaction, err: Exception):
    """
    技術用語を避けたフレンドリーなエラー文をエフェメラルで返す。
    （429や一時障害を含む全ての想定外エラー）
    """
    msg = "We’re a bit busy right now. Please try again in about a minute."
    try:
        # 429/5xx のときも同じ文言でよい（ユーザー混乱回避）
        if interaction.response.is_done():
            await interaction.followup.send(content=msg, ephemeral=True)
        else:
            await interaction.response.send_message(content=msg, ephemeral=True)
    except Exception:
        # 最後の保険（失敗してもログだけ）
        print(f"[friendly_error] {type(err).__name__}: {err}")

# ========= Sheets helpers (429-safe) =========
WALLET_SHEET_MAP = {1: "wallet_log", 2: "wallet_log2", 3: "wallet_log3"}
ALL_WALLET_SHEETS = ["wallet_log", "wallet_log2", "wallet_log3"]
MASTER_SHEET = "wallet_master"  # 唯一ウォレットの保管先

_ws_cache: Dict[str, gspread.Worksheet] = {}
_values_cache: Dict[Tuple[str, str], Any] = {}  # (sheet_name, "all") -> values

def sheets_call(func: Callable, *args, **kwargs):
    """
    Sheets API 呼び出しラッパ（429/5xx 対策の指数バックオフ）。
    """
    delay = 0.5
    for attempt in range(4):
        try:
            return func(*args, **kwargs)
        except APIError as e:
            code = getattr(e, "response", None).status_code if hasattr(e, "response") and e.response else None
            if code in (429, 500, 502, 503, 504):
                time.sleep(delay)
                delay *= 2
                continue
            raise
        except Exception:
            raise
    return func(*args, **kwargs)

def _get_ws(spreadsheet: gspread.Spreadsheet, title: str, create: bool = False) -> gspread.Worksheet:
    if title in _ws_cache:
        return _ws_cache[title]
    try:
        ws = sheets_call(spreadsheet.worksheet, title)
    except WorksheetNotFound:
        if not create:
            raise
        ws = sheets_call(spreadsheet.add_worksheet, title=title, rows=1000, cols=10)
    _ws_cache[title] = ws
    return ws

def _get_all_values(ws: gspread.Worksheet):
    # bindings は“外部で手動更新”が起きるため必ず最新を取得（キャッシュしない）
    if ws.title == "bindings":
        return sheets_call(ws.get_all_values)

    key = (ws.title, "all")
    if key in _values_cache:
        return _values_cache[key]
    vals = sheets_call(ws.get_all_values)
    _values_cache[key] = vals
    return vals

def _find_row_by_id(ws: gspread.Worksheet, user_id: str):
    # Force-refresh this sheet once right before reading (avoid showing stale values)
    _values_cache.pop((ws.title, "all"), None)

    values = _get_all_values(ws)
    for idx, row in enumerate(values, start=1):
        if len(row) >= 2 and row[1] == user_id:
            return idx, row
    return None, None

def _lookup_wallet_in_sheet(ws: gspread.Worksheet, user_id: str) -> Tuple[Optional[str], Optional[str]]:
    idx, row = _find_row_by_id(ws, user_id)
    if idx and len(row) >= 3:
        return (row[0], row[2])
    return (None, None)

def _upsert_wallet(ws: gspread.Worksheet, user_name: str, user_id: str, wallet: str):
    idx, row = _find_row_by_id(ws, user_id)
    if idx:
        sheets_call(ws.update_cell, idx, 1, user_name)
        sheets_call(ws.update_cell, idx, 2, user_id)
        sheets_call(ws.update_cell, idx, 3, wallet)
    else:
        sheets_call(ws.append_row, [user_name, user_id, wallet], value_input_option="RAW")
    _values_cache.pop((ws.title, "all"), None)

# --- Master operations ---
def _get_master_ws() -> gspread.Worksheet:
    return _get_ws(sh, MASTER_SHEET, create=True)

def get_master_wallet(user_id: str) -> Tuple[Optional[str], Optional[str]]:
    ws = _get_master_ws()
    return _lookup_wallet_in_sheet(ws, user_id)

def set_master_wallet(user_name: str, user_id: str, wallet: str):
    ws = _get_master_ws()
    _upsert_wallet(ws, user_name, user_id, wallet)

# --- Event sheet operations ---
def enroll_in_sheet_only(sheet_name: str, user_name: str, user_id: str, wallet: str):
    ws = _get_ws(sh, sheet_name, create=True)
    _upsert_wallet(ws, user_name, user_id, wallet)

def update_existing_sheets(user_name: str, user_id: str, wallet: str):
    for s in ALL_WALLET_SHEETS:
        ws = _get_ws(sh, s, create=True)
        idx, _ = _find_row_by_id(ws, user_id)
        if idx:
            sheets_call(ws.update_cell, idx, 1, user_name)
            sheets_call(ws.update_cell, idx, 2, user_id)
            sheets_call(ws.update_cell, idx, 3, wallet)
            _values_cache.pop((ws.title, "all"), None)

# ========= Bindings (snapshot_bot_log.bindings) =========
def _get_bindings_ws() -> gspread.Worksheet:
    try:
        return _get_ws(sh, "bindings", create=False)
    except WorksheetNotFound:
        ws = _get_ws(sh, "bindings", create=True)
        sheets_call(ws.append_row, ["GuildID", "ChannelID", "MessageID", "SheetName", "CreatedAtISO"], value_input_option="RAW")
        return ws

def _is_sheet_already_bound(guild_id: int, sheet_name: str) -> bool:
    ws = _get_bindings_ws()
    # ここはキャッシュを使わず常に最新を確認
    for row in sheets_call(ws.get_all_values)[1:]:
        if len(row) >= 4 and row[0] == str(guild_id) and row[3] == sheet_name:
            return True
    return False

def _get_binding_record(guild_id: int, sheet_name: str):
    ws = _get_bindings_ws()
    for row in sheets_call(ws.get_all_values)[1:]:
        if len(row) >= 4 and row[0] == str(guild_id) and row[3] == sheet_name:
            return {
                "guild_id": int(row[0]),
                "channel_id": int(row[1]),
                "message_id": int(row[2]),
                "sheet_name": row[3],
                "created_at": row[4] if len(row) > 4 else ""
            }
    return None

def _add_binding(guild_id: int, channel_id: int, message_id: int, sheet_name: str):
    ws = _get_bindings_ws()
    sheets_call(ws.append_row, [str(guild_id), str(channel_id), str(message_id), sheet_name, datetime.utcnow().isoformat()], value_input_option="RAW")
    _values_cache.pop((ws.title, "all"), None)

def _get_binding_by_message(message_id: int):
    ws = _get_bindings_ws()
    for row in _get_all_values(ws)[1:]:
        if len(row) >= 3 and row[2] == str(message_id):
            return int(row[0]), int(row[1]), row[3]
    return None

def _list_bindings_for_guild(guild_id: int):
    ws = _get_bindings_ws()
    out = []
    for row in _get_all_values(ws)[1:]:
        if len(row) >= 5 and row[0] == str(guild_id):
            out.append({
                "guild_id": int(row[0]),
                "channel_id": int(row[1]),
                "message_id": int(row[2]),
                "sheet_name": row[3],
                "created_at": row[4],
            })
    return out

# ========= Snapshot (existing) =========
class SnapshotCog(commands.Cog):
    def __init__(self, bot): self.bot = bot

    @app_commands.command(name="snapshot", description="Fetch token holder info for a contract address (ephemeral).")
    @app_commands.describe(contract_address="Enter the token contract address")
    async def snapshot(self, interaction: discord.Interaction, contract_address: str):
        await interaction.response.defer(ephemeral=True)
        progress_message = await interaction.followup.send(content="Fetching token holders... (page 1)", ephemeral=True)
        try:
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
            sheets_call(worksheet.append_row, [str(interaction.user), contract_address, str(total_holders), str(total_supply)], value_input_option="RAW")
            await progress_message.edit(content="Snapshot completed! Sending file...")
            await interaction.followup.send(content=summary, ephemeral=True,
                                            file=discord.File(fp=io.StringIO(csv_buffer.getvalue()), filename="holderList.csv"))
        except Exception as e:
            await send_friendly_error(interaction, e)

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
            await send_friendly_error(interaction, e)

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
        try:
            user_name = self.user_name_override or str(interaction.user)
            user_id = str(interaction.user.id)
            new_wallet = self.wallet_input.value.strip()

            if self.is_change:
                set_master_wallet(user_name, user_id, new_wallet)
                update_existing_sheets(user_name, user_id, new_wallet)
                await interaction.response.send_message(
                    content=f"✅ Wallet changed to **{new_wallet}**\n**User**: {user_name} (updated where you were already enrolled)",
                    ephemeral=True
                )
            else:
                enroll_in_sheet_only(self.sheet_name, user_name, user_id, new_wallet)
                set_master_wallet(user_name, user_id, new_wallet)
                await interaction.response.send_message(
                    content=f"✅ Registration completed.\n**User**: {user_name}\n**Wallet**: {new_wallet}",
                    ephemeral=True
                )
        except Exception as e:
            await send_friendly_error(interaction, e)

class WalletHubView(discord.ui.View):
    """3 buttons bound to a specific sheet; optional auto-enroll on any button."""
    def __init__(self):
        super().__init__(timeout=None)

    def _bound_sheet(self, interaction: discord.Interaction) -> str:
        binding = _get_binding_by_message(interaction.message.id)
        if not binding:
            raise RuntimeError("No binding for this message.")
        return binding[2]

    async def _maybe_auto_enroll_from_master(self, sheet: str, user_name: str, user_id: str) -> Tuple[bool, Optional[str], Optional[str]]:
        m_name, m_wallet = get_master_wallet(user_id)
        if not m_wallet:
            return False, None, None
        if not AUTO_ENROLL_FROM_MASTER_ON_ANY_BUTTON:
            return False, m_name, m_wallet
        enroll_in_sheet_only(sheet, m_name or user_name, user_id, m_wallet)
        return True, m_name or user_name, m_wallet

    @discord.ui.button(label="Register wallet", style=discord.ButtonStyle.primary, row=0)  # 青
    async def btn_register(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            sheet = self._bound_sheet(interaction)
            user_name, user_id = str(interaction.user), str(interaction.user.id)

            ws = _get_ws(sh, sheet, create=True)
            s_name, s_wallet = _lookup_wallet_in_sheet(ws, user_id)
            if s_wallet:
                await interaction.response.send_message(
                    content=f"📝 Already submitted here.\n**User**: {s_name}\n**Wallet**: {s_wallet}",
                    ephemeral=True
                ); return

            m_name, m_wallet = get_master_wallet(user_id)
            if m_wallet:
                enroll_in_sheet_only(sheet, m_name or user_name, user_id, m_wallet)
                await interaction.response.send_message(
                    content=f"✅ Synced from your master record.\n**User**: {m_name or user_name}\n**Wallet**: {m_wallet}",
                    ephemeral=True
                ); return

            await interaction.response.send_modal(RegisterOrChangeWalletModal(sheet, preset_wallet="", is_change=False, user_name=user_name))
        except Exception as e:
            await send_friendly_error(interaction, e)

    @discord.ui.button(label="Check wallet", style=discord.ButtonStyle.success, row=0)  # 緑
    async def btn_check(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            sheet = self._bound_sheet(interaction)
            user_name, user_id = str(interaction.user), str(interaction.user.id)

            ws = _get_ws(sh, sheet, create=True)
            s_name, s_wallet = _lookup_wallet_in_sheet(ws, user_id)
            if s_wallet:
                await interaction.response.send_message(content=f"**User**: {s_name}\n**Wallet**: {s_wallet}", ephemeral=True); return

            enrolled, name, wal = await self._maybe_auto_enroll_from_master(sheet, user_name, user_id)
            if enrolled:
                await interaction.response.send_message(
                    content=f"✅ Enrolled here from your master record.\n**User**: {name}\n**Wallet**: {wal}",
                    ephemeral=True
                ); return

            m_name, m_wallet = get_master_wallet(user_id)
            if m_wallet:
                await interaction.response.send_message(
                    content=(f"Not registered in this list yet.\n"
                             f"Master record:\n**User**: {m_name}\n**Wallet**: {m_wallet}"),
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(content="No wallet found. Please register first.", ephemeral=True)
        except Exception as e:
            await send_friendly_error(interaction, e)

    @discord.ui.button(label="Change wallet", style=discord.ButtonStyle.danger, row=0)  # 赤
    async def btn_change(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            sheet = self._bound_sheet(interaction)
            user_name, user_id = str(interaction.user), str(interaction.user.id)

            ws = _get_ws(sh, sheet, create=True)
            s_name, s_wallet = _lookup_wallet_in_sheet(ws, user_id)

            if not s_wallet:
                enrolled, name, wal = await self._maybe_auto_enroll_from_master(sheet, user_name, user_id)
                if enrolled:
                    s_name, s_wallet = name, wal

            if not s_wallet:
                m_name, m_wallet = get_master_wallet(user_id)
                if m_wallet:
                    await interaction.response.send_message(
                        content=(f"Not registered in this list yet.\n"
                                 f"Master record:\n**User**: {m_name}\n**Wallet**: {m_wallet}"),
                        ephemeral=True
                    )
                else:
                    await interaction.response.send_message(content="No wallet found. Please register first.", ephemeral=True)
                return

            msg = f"Current wallet: **{s_wallet}**\nProceed to change?"
            await interaction.response.send_message(
                content=msg, ephemeral=True,
                view=ConfirmChangeView(sheet, current_wallet=s_wallet, user_name=s_name or user_name)
            )
        except Exception as e:
            await send_friendly_error(interaction, e)

class WalletHub(commands.Cog):
    def __init__(self, bot): self.bot = bot

    @app_commands.command(
        name="register_wallet",
        description="Admin only: Post (or refresh) a wallet hub bound to a specific sheet (1=wallet_log, 2=wallet_log2, 3=wallet_log3)."
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        channel="Target channel",
        button_number="1=wallet_log, 2=wallet_log2, 3=wallet_log3",
        edit_if_exists="If a binding exists, refresh its buttons instead of error (default: False)"
    )
    async def register_wallet(self, interaction: discord.Interaction, channel: discord.TextChannel,
                              button_number: app_commands.Range[int, 1, 3],
                              edit_if_exists: bool = False):
        await interaction.response.defer(ephemeral=True)
        try:
            sheet_name = _sheet_from_button_number(button_number)
            exists = _is_sheet_already_bound(interaction.guild_id, sheet_name)

            if exists and not edit_if_exists:
                await interaction.followup.send(content=f"❌ Binding already exists for **{sheet_name}**.", ephemeral=True)
                return

            if exists and edit_if_exists:
                # 既存メッセージを編集してボタンを復旧
                rec = _get_binding_record(interaction.guild_id, sheet_name)
                if not rec:
                    await interaction.followup.send(content="Binding record not found. Please re-create.", ephemeral=True)
                    return
                # 既存メッセージを取得して view 差し替え
                try:
                    target_ch = channel if channel.id == rec["channel_id"] else await bot.fetch_channel(rec["channel_id"])
                    target_msg = await target_ch.fetch_message(rec["message_id"])
                    await target_msg.edit(view=WalletHubView())  # 画像や本文はそのまま、ボタンだけ復旧
                    await interaction.followup.send(content=f"✅ Refreshed buttons for **{sheet_name}**.", ephemeral=True)
                except Exception as e:
                    await send_friendly_error(interaction, e)
                return

            # 新規設置
            _get_ws(sh, sheet_name, create=True)  # ensure exists

            embed = discord.Embed(
                title="Wallet Center",
                description="Register, check, or change your wallet.\nAll actions are ephemeral (visible only to you).",
                color=0x836EF9
            )
            embed.set_footer(text="Secure • Fast • Private")
            file = discord.File(EMBED_IMAGE_PATH, filename="C_logo.png") if os.path.exists(EMBED_IMAGE_PATH) else None
            if file: embed.set_thumbnail(url="attachment://C_logo.png")

            view = WalletHubView()
            msg = await (channel.send(embed=embed, view=view, file=file) if file else channel.send(embed=embed, view=view))
            _add_binding(interaction.guild_id, channel.id, msg.id, sheet_name)
            await interaction.followup.send(content=f"✅ Posted wallet hub in {channel.mention} (bound to **{sheet_name}**).", ephemeral=True)

        except Exception as e:
            await send_friendly_error(interaction, e)

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
        try:
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
            embed.set_footer(text="Use /register_wallet (admin) to add/refresh bindings.")
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await send_friendly_error(interaction, e)

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

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
