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
worksheet = sh.worksheet("log")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

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
