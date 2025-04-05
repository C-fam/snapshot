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
        NFTのホルダーを最大10000人分取得します。
        大量にいる場合は時間がかかるのでご注意ください。
        (0.5秒間隔でAPIを呼び出します)
        """
        # Defer the response so it remains ephemeral until final output
        await interaction.response.defer(ephemeral=True)

        # 進捗表示用のメッセージを最初に送っておく
        progress_message = await interaction.followup.send(
            content="Fetching token holders... (page 1)",
            ephemeral=True
        )

        base_url = "https://api.socialscan.io/monad-testnet/v1/developer/api"
        module = "token"
        action = "tokenholderlist"
        offset = 100
        page = 1

        # --- 1) リストで全ホルダー情報を重複含めて蓄積 ---
        all_holders = []

        # --- 停止しにくい仕組み: 連続エラーをカウントし, 一定回数超えたら終了 ---
        max_consecutive_errors = 5
        error_count = 0

        # --- 10000ホルダーまでを上限とする ---
        max_holders = 10000

        while True:
            # 今どのページを読み込んでいるかを表示 (editで更新)
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

            # エラーの場合でも簡単に停止しないようにする
            if response.status_code != 200:
                error_count += 1
                if error_count >= max_consecutive_errors:
                    # エラーが続きすぎたら中断
                    break
                time.sleep(0.5)
                continue  # 同じページをリトライ

            data = response.json()

            # APIのステータスが "1" 以外 or 結果がない場合 (多少のエラーならすぐ停止しない)
            if data.get("status") != "1":
                error_count += 1
                if error_count >= max_consecutive_errors:
                    break
                time.sleep(0.6)
                continue  # 同じページをリトライ
            else:
                # 成功したらエラー連続カウントをリセット
                error_count = 0

            result_list = data.get("result")
            if not result_list:
                # データが空 (最後まで取得したかAPIが何も返さない)
                break

            for holder in result_list:
                address = holder["TokenHolderAddress"]
                quantity_float = float(holder["TokenHolderQuantity"])
                all_holders.append((address, quantity_float))

                # 10000 ホルダーで打ち切り
                if len(all_holders) >= max_holders:
                    break

            # もし10000件到達したら打ち切り
            if len(all_holders) >= max_holders:
                break

            # 取得結果の数がオフセットより小さかったら最終ページ
            if len(result_list) < offset:
                break

            # 次のページへ
            page += 1

            # リクエスト間隔 0.5秒
            time.sleep(0.5)

        # --- 2) 合計サプライを整数で計算 ---
        total_supply = int(sum(quantity for _, quantity in all_holders))

        # --- 3) ホルダー数（重複込み） ---
        total_holders = len(all_holders)

        # --- 4) CSVを作成 ---
        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["TokenHolderAddress", "TokenHolderQuantity"])

        for address, quantity_float in all_holders:
            quantity_str = str(int(quantity_float))  # 小数は切り捨て
            writer.writerow([address, quantity_str])

        csv_buffer.seek(0)

        # --- 5) 表示用メッセージ作成 ---
        summary_text = (
            f"**Contract Address**: {contract_address}\n"
            f"**Total Holders**: {total_holders} (up to {max_holders})\n"
            f"**Total Supply**: {total_supply}\n\n"
            "Your CSV file is attached below.\n"
            "※最大10000ホルダーまで対応しています。"
        )

        # --- 6) Google Sheets ログに記録 ---
        user_name = str(interaction.user)
        worksheet.append_row(
            [user_name, contract_address, str(total_holders), str(total_supply)],
            value_input_option="RAW"
        )

        # --- 7) CSVファイルを添付して返信 ---
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
