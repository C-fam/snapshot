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
        # Defer the response so it remains ephemeral until final output
        await interaction.response.defer(ephemeral=True)

        base_url = "https://api.socialscan.io/monad-testnet/v1/developer/api"
        module = "token"
        action = "tokenholderlist"
        offset = 100
        page = 1

        # --- 1) リストで全ホルダー情報を重複含めて蓄積 ---
        all_holders = []

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

            # If the API status is not "1" or there's no result, break
            if data.get("status") != "1" or not data.get("result"):
                break

            result_list = data["result"]

            for holder in result_list:
                address = holder["TokenHolderAddress"]
                quantity_float = float(holder["TokenHolderQuantity"])
                # リストに単純追加 (重複アドレスもすべて追加)
                all_holders.append((address, quantity_float))

            # If fewer than 'offset' holders are returned, we've reached the last page
            if len(result_list) < offset:
                break

            page += 1
            # Delayは不要なので削除

        # --- 2) 合計サプライを整数で計算 ---
        # 小数部は単純に切り捨て（int()）: 3.8 -> 3,  10.0 -> 10 など
        total_supply = int(sum(quantity for _, quantity in all_holders))

        # --- 3) ホルダー数（重複込み） ---
        total_holders = len(all_holders)

        # --- 4) CSVを作成 ---
        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["TokenHolderAddress", "TokenHolderQuantity"])

        for address, quantity_float in all_holders:
            # ホルダーごとの数量を整数に変換
            quantity_str = str(int(quantity_float))  # 3.0 や 3.8 -> "3"
            writer.writerow([address, quantity_str])

        csv_buffer.seek(0)

        # --- 5) 表示用メッセージ作成 (小数点なし) ---
        summary_text = (
            f"**Contract Address**: {contract_address}\n"
            f"**Total Holders**: {total_holders}\n"
            f"**Total Supply**: {total_supply}\n\n"
            "Your CSV file is attached below."
        )

        # --- 6) Google Sheets ログに記録 ---
        # ここもサプライは小数点なしで記録
        user_name = str(interaction.user)
        worksheet.append_row(
            [user_name, contract_address, str(total_holders), str(total_supply)],
            value_input_option="RAW"
        )

        # --- 7) CSVファイルを添付して返信 ---
        file_to_send = discord.File(fp=csv_buffer, filename="holderList.csv")
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
