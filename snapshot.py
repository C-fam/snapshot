import os
import io
import csv
import json
import requests
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime

# --------------------------
# 1. Load environment variables
# --------------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_BASE_URL = "https://api.socialscan.io/monad-testnet/v1/developer/api"
API_KEY = os.getenv("API_KEY")

# Read Google service account credentials from .env
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS")
if not GOOGLE_CREDENTIALS:
    raise ValueError("GOOGLE_CREDENTIALS not found in .env")

# --------------------------
# 2. Set up Google Sheets client
# --------------------------
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

try:
    creds_dict = json.loads(GOOGLE_CREDENTIALS)
except json.JSONDecodeError as e:
    raise ValueError(f"Failed to parse GOOGLE_CREDENTIALS: {e}")

credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, SCOPE)
gspread_client = gspread.authorize(credentials)

try:
    # Open your spreadsheet by name (no SPREADSHEET_ID needed)
    # Make sure the sheet name "snapshot_bot_log" exists in your account
    SPREADSHEET = gspread_client.open("snapshot_bot_log")
except Exception as e:
    raise ValueError(f"Failed to open 'snapshot_bot_log': {e}")

# We'll write logs to a worksheet named "log"
WORKSHEET_NAME = "log"

# --------------------------
# 3. Discord bot setup
# --------------------------
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


# --------------------------
# 4. Helper functions
# --------------------------
def fetch_all_token_holders(contract_address: str, offset: int = 100):
    """
    Repeatedly fetch holders from the API in pages until there's no more data or we hit a limit.
    Returns a list of dicts: [{TokenHolderAddress, TokenHolderQuantity}, ...]
    """
    all_holders = []
    page = 1

    # OPTIONAL: If you want to limit to 300 pages (30,000 holders), uncomment next line and check logic below
    # MAX_PAGES = 300

    while True:
        url = (
            f"{API_BASE_URL}?module=token&action=tokenholderlist"
            f"&contractaddress={contract_address}"
            f"&page={page}&offset={offset}&apikey={API_KEY}"
        )
        resp = requests.get(url)
        if resp.status_code != 200:
            print(f"HTTP Error {resp.status_code}")
            break

        data = resp.json()
        if data.get("status") != "1" or data.get("message") != "OK":
            break

        result = data.get("result", [])
        if not result:
            break

        for holder in result:
            all_holders.append({
                "TokenHolderAddress": holder.get("TokenHolderAddress"),
                "TokenHolderQuantity": holder.get("TokenHolderQuantity")
            })

        page += 1

        # If you want to enforce up to 300 pages, uncomment below:
        # if page > MAX_PAGES:
        #    break

    return all_holders


def create_csv_in_memory(holders):
    """
    Create an in-memory CSV from the holder list.
    Returns a BytesIO object suitable for Discord file attachment.
    """
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["TokenHolderAddress", "TokenHolderQuantity"])
    writer.writeheader()
    for h in holders:
        writer.writerow(h)

    # Convert to BytesIO so Discord can attach it
    csv_bytes = io.BytesIO(output.getvalue().encode("utf-8"))
    return csv_bytes


def log_to_google_sheet(contract_address: str, holder_count: int, total_supply: float):
    """
    Append a row to the 'log' worksheet in 'snapshot_bot_log'.
    Columns: [Timestamp, ContractAddress, HolderCount, TotalSupply]
    """
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    try:
        worksheet = SPREADSHEET.worksheet(WORKSHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        # Create the worksheet if it doesn't exist
        worksheet = SPREADSHEET.add_worksheet(title=WORKSHEET_NAME, rows="1000", cols="10")

    # Append row to the bottom of the sheet
    worksheet.append_row([now_str, contract_address, str(holder_count), str(total_supply)])


# --------------------------
# 5. Slash command
# --------------------------
@bot.slash_command(name="snapshot", description="Get holder list, total supply, and create an ephemeral CSV attachment.")
async def snapshot_command(ctx: discord.ApplicationContext, contract_address: str):
    """
    Fetches all token holders for the given contract, calculates total supply & holder count,
    logs to Google Sheets, and returns an ephemeral message with a CSV attachment.
    """
    await ctx.defer(ephemeral=True)

    # 1) Fetch data from the API
    holders = fetch_all_token_holders(contract_address)
    holder_count = len(holders)
    total_supply = sum(float(h["TokenHolderQuantity"]) for h in holders)

    # 2) Create a CSV in memory
    csv_file = create_csv_in_memory(holders)

    # 3) Log usage to Google Sheets
    #    (You might want to run this in a separate thread if you're worried about blocking,
    #    but for simplicity, we do it directly.)
    log_to_google_sheet(contract_address, holder_count, total_supply)

    # 4) Prepare ephemeral message
    message_content = (
        f"**Contract Address**: {contract_address}\n"
        f"**Holder Count**: {holder_count}\n"
        f"**Total Supply**: {total_supply}"
    )

    file = discord.File(csv_file, filename="holderList.csv")
    await ctx.respond(content=message_content, file=file, ephemeral=True)


# --------------------------
# 6. Bot event: on_ready
# --------------------------
@bot.event
async def on_ready():
    print(f"Bot is online as {bot.user}")
    # Sync slash commands (so they appear in Discord)
    try:
        await bot.tree.sync()
        print("Slash commands synced!")
    except Exception as e:
        print(f"Error syncing slash commands: {e}")


# --------------------------
# 7. Main entry
# --------------------------
if __name__ == "__main__":
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN not found in .env.")
    if not API_KEY:
        raise ValueError("API_KEY not found in .env.")

    bot.run(BOT_TOKEN)
