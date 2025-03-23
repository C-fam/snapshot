# bot.py (example)
import os
import io
import csv
import json
import requests
import discord

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
# We assume the user has "snapshot_bot_log" spreadsheet with a "log" sheet.
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_dict = json.loads(SERVICE_ACCOUNT_INFO)
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gc = gspread.authorize(creds)

# Attempt to open the spreadsheet and select the "log" sheet
sh = gc.open("snapshot_bot_log")
worksheet = sh.worksheet("log")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# We'll use app_commands (slash commands) within a Cog
class SnapshotCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="snapshot",
        description="Fetch token holder info for a contract address (ephemeral)."
    )
    @app_commands.describe(contract_address="Enter the token contract address")
    async def snapshot(self, interaction: discord.Interaction, contract_address: str):
        # Defer the response to keep it ephemeral until we send the final result
        await interaction.response.defer(ephemeral=True)

        base_url = "https://api.socialscan.io/monad-testnet/v1/developer/api"
        module = "token"
        action = "tokenholderlist"
        offset = 100
        page = 1

        all_holders = []
        total_supply = 0

        while True:
            params = {
                "module": module,
                "action": action,
                "contractaddress": contract_address,
                "page": page,
                "offset": offset,
                "apikey": API_KEY
            }
            response = requests.get(base_url, params=params)
            data = response.json()

            # If the API status is not "1" or result is empty, break out
            if data.get("status") != "1" or not data.get("result"):
                break

            result_list = data["result"]

            for holder in result_list:
                address = holder["TokenHolderAddress"]
                quantity_str = holder["TokenHolderQuantity"]
                quantity = float(quantity_str)
                all_holders.append((address, quantity))
                total_supply += quantity

            # Check how many items were returned
            if len(result_list) < offset:
                break

            page += 1

            # [Optional] Limit to 30000 holders (300 pages) - disabled by default
            # if len(all_holders) >= 30000:
            #    break

        # Create an in-memory CSV
        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["TokenHolderAddress", "TokenHolderQuantity"])
        for address, qty in all_holders:
            writer.writerow([address, qty])

        csv_buffer.seek(0)  # Reset pointer to the beginning of the CSV data

        # Prepare the ephemeral response content
        total_holders = len(all_holders)
        summary_text = (
            f"**Contract Address**: {contract_address}\n"
            f"**Total Holders**: {total_holders}\n"
            f"**Total Supply**: {total_supply}\n\n"
            "Your CSV file is attached below."
        )

        # Log to Google Sheets (append a new row)
        # Example of logging: [Username, Contract, HolderCount, Supply]
        user_name = str(interaction.user)
        worksheet.append_row(
            [user_name, contract_address, str(total_holders), str(total_supply)],
            value_input_option="RAW"
        )

        # Attach the CSV as a file
        file_to_send = discord.File(fp=csv_buffer, filename="holderList.csv")

        await interaction.followup.send(
            content=summary_text,
            ephemeral=True,
            file=file_to_send
        )

async def setup_bot():
    await bot.add_cog(SnapshotCog(bot))
    # Sync application commands with Discord
    # so that slash commands appear.
    await bot.tree.sync()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")
    # Sync commands each time the bot starts (optional, or move to a manual command)
    await bot.wait_until_ready()
    await setup_bot()

bot.run(DISCORD_TOKEN)
