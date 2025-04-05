import os
import io
import csv
import json
import asyncio
import aiohttp  # 並列リクエスト用
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


async def fetch_page(session, base_url, contract_address, page, offset, api_key):
    """
    指定した page のデータを非同期で取得し、JSON を返す。
    エラーがあれば None を返す。
    """
    params = {
        "module": "token",
        "action": "tokenholderlist",
        "contractaddress": contract_address,
        "page": page,
        "offset": offset,
        "apikey": api_key
    }
    try:
        async with session.get(base_url, params=params) as resp:
            # status 200 以外なら失敗とみなす
            if resp.status != 200:
                return None
            data = await resp.json()
            # "status" が "1" でないなら失敗
            if data.get("status") != "1":
                return None
            return data
    except Exception:
        return None


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
        NFTのホルダーを最大10000人分、ページ分割で(ある程度)並列取得します。
        大量にいる場合は時間がかかるのでご注意ください。
        """
        await interaction.response.defer(ephemeral=True)

        # 進捗表示用のメッセージ
        progress_message = await interaction.followup.send(
            content="Initializing...",
            ephemeral=True
        )

        base_url = "https://api.socialscan.io/monad-testnet/v1/developer/api"
        offset = 100        # 1ページあたり 100 件
        max_holders = 10000 # 10000 ホルダーまで
        all_holders = []

        # 非同期 HTTP セッションを使う
        async with aiohttp.ClientSession() as session:
            # 何ページまで取得するか分からないので、ページ番号をインクリメントしながら進む
            page = 1
            consecutive_empty = 0  # 空ページが連続したら打ち切り
            batch_size = 10        # 1度に並列実行するページ数(あまり多くしすぎると負荷がかかる)
            
            while len(all_holders) < max_holders:
                # 進捗表示
                await progress_message.edit(
                    content=f"Fetching pages {page} ~ {page+batch_size-1} ... (collected {len(all_holders)} holders so far)"
                )

                # まとめてページを投げる
                tasks = []
                for p in range(page, page + batch_size):
                    tasks.append(
                        fetch_page(session, base_url, contract_address, p, offset, API_KEY)
                    )

                results = await asyncio.gather(*tasks)
                
                # まとめて返ってきた結果を処理
                any_new_data = False
                for data in results:
                    # fetch_page が None を返したら失敗またはデータなし
                    if (data is None) or (not data.get("result")):
                        consecutive_empty += 1
                        # 2連続で空ならもう無いとみなしてブレイク
                        if consecutive_empty >= 2:
                            break
                        continue
                    else:
                        consecutive_empty = 0  # リセット

                    # result にホルダー一覧が入っている
                    result_list = data.get("result", [])
                    for holder in result_list:
                        address = holder["TokenHolderAddress"]
                        quantity_float = float(holder["TokenHolderQuantity"])
                        all_holders.append((address, quantity_float))
                        
                        # 10000ホルダーに達したら打ち切り
                        if len(all_holders) >= max_holders:
                            break
                    if len(all_holders) >= max_holders:
                        break
                    
                    # result_list が offset (=100) より少ない場合は最後のページと推定
                    if len(result_list) < offset:
                        consecutive_empty += 1
                    else:
                        any_new_data = True

                    if consecutive_empty >= 2 or len(all_holders) >= max_holders:
                        break

                # 大量ページの処理が終了 or limit到達
                if not any_new_data or consecutive_empty >= 2 or len(all_holders) >= max_holders:
                    break

                page += batch_size
                # 過剰な速さで叩きすぎないよう、少しウェイト
                # (まとめて叩いているので pageごとのsleep 0.5sec×10=5sec と同等くらい)
                await asyncio.sleep(1.0)

        # ---- 取得終了 ----
        total_supply = int(sum(qty for _, qty in all_holders))
        total_holders = len(all_holders)

        # CSV 作成
        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["TokenHolderAddress", "TokenHolderQuantity"])
        for address, quantity_float in all_holders:
            writer.writerow([address, str(int(quantity_float))])
        csv_buffer.seek(0)

        summary_text = (
            f"**Contract Address**: {contract_address}\n"
            f"**Total Holders**: {total_holders} (up to {max_holders})\n"
            f"**Total Supply**: {total_supply}\n\n"
            "Your CSV file is attached below.\n"
            "※最大10000ホルダーまで対応しています。\n"
            "**(Parallel fetch)**"
        )

        # ログ記録
        user_name = str(interaction.user)
        worksheet.append_row(
            [user_name, contract_address, str(total_holders), str(total_supply)],
            value_input_option="RAW"
        )

        # 送信
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
