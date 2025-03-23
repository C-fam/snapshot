import discord
from discord.ext import commands
import requests
import io
import csv
import os
import datetime

# For environment variables
from dotenv import load_dotenv

# For Google Sheets logging
import gspread
from google.oauth2.service_account import Credentials

# Load .env
load_dotenv()

# Environment variables
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
API_KEY = os.getenv("API_KEY")
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")

# Create bot instance
bot = commands.Bot(
    command_prefix="!",  # Not necessary for slash commands but can keep for compatibility
    intents=discord.Intents.default()
)

# Google Sheets setup
def get_gspread_client():
    """
    Authorize and return a gspread client using a service account JSON file.
    The JSON file name/path is stored in GOOGLE_SERVICE_ACCOUNT_FILE.
    """
    credentials = Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_FILE)
    gc = gspread.authorize(credentials)
    return gc

def append_log_to_spreadsheet(contract_address, holder_count, total_supply):
    """
    Append a log entry to the 'snapshot_bot_log' spreadsheet, 'log' worksheet.
    Columns example: [Timestamp, ContractAddress, HolderCount, TotalSupply]
    """
    gc = get_gspread_client()
    # Open spreadsheet by name and select the worksheet named 'log'
    sheet = gc.open("snapshot_bot_log").worksheet("log")

    # Prepare a row to insert
    current_time = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    row_data = [current_time, contract_address, holder_count, total_supply]
    sheet.append_row(row_data)

# Constants
API_BASE_URL = "https://api.socialscan.io/monad-testnet/v1/developer/api"
OFFSET = 100  # Number of records per page

def fetch_all_token_holders(contract_address, offset=100):
    """
    Fetch all token holders across multiple pages.
    Returns a list of dicts: [{TokenHolderAddress, TokenHolderQuantity}, ...]
    """
    all_holders = []
    page = 1

    while True:
        url = (
            f"{API_BASE_URL}?module=token&action=tokenholderlist"
            f"&contractaddress={contract_address}"
            f"&page={page}&offset={offset}&apikey={API_KEY}"
        )
        response = requests.get(url)
        if response.status_code != 200:
            print(f"HTTP Error: {response.status_code}")
            break

        data = response.json()
        if data.get("status") != "1" or data.get("message") != "OK":
            break

        result = data.get("result", [])
        if not result:
            # No more data
            break

        for holder in result:
            all_holders.append({
                "TokenHolderAddress": holder.get("TokenHolderAddress"),
                "TokenHolderQuantity": holder.get("TokenHolderQuantity")
            })

        page += 1

        # Uncomment below if you want to limit to 300 pages (30,000 records)
        # if page > 300:
        #     print("Page limit of 300 reached. Stopping data fetch.")
        #     break

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

    csv_bytes = io.BytesIO(output.getvalue().encode("utf-8"))
    return csv_bytes

@bot.slash_command(
    name="snapshot",
    description="Fetch holder info for a given contract address."
)
async def snapshot(ctx: discord.ApplicationContext, contract_address: str):
    """
    Slash command to retrieve holders, total supply, and holder count for a contract address.
    Sends an ephemeral response with a CSV file attachment and logs to Google Sheets.
    """
    await ctx.defer(ephemeral=True)

    holders = fetch_all_token_holders(contract_address, OFFSET)
    holder_count = len(holders)
    total_supply = sum(float(h["TokenHolderQuantity"]) for h in holders)

    # Build a CSV in memory
    csv_file = create_csv_in_memory(holders)

    # Create a response message
    message_content = (
        f"**Contract Address**: {contract_address}\n"
        f"**Holder Count**: {holder_count}\n"
        f"**Total Supply**: {total_supply}"
    )

    # Send ephemeral response with the CSV attachment
    file = discord.File(csv_file, filename="holderList.csv")
    await ctx.respond(
        content=message_content,
        file=file,
        ephemeral=True
    )

    # Log the result in Google Spreadsheet
    append_log_to_spreadsheet(contract_address, holder_count, total_supply)

# Run the bot
bot.run(BOT_TOKEN)
