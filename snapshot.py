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

# .env から読み込む
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
API_KEY = os.getenv("API_KEY")
SERVICE_ACCOUNT_INFO = os.getenv("SERVICE_ACCOUNT_INFO")

# Google Sheets 初期化
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_dict = json.loads(SERVICE_ACCOUNT_INFO)
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gc = gspread.authorize(creds)
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
    @app_commands.describe(
        contract_address="Enter the token (or NFT) contract address",
        erc="Specify '20' for ERC20, '721' for ERC721, or '1155' for ERC1155"
    )
    async def snapshot(self, interaction: discord.Interaction, contract_address: str, erc: str):
        """
        Discordコマンド: /snapshot [contract_address] [erc]
        - ERC20 / ERC721 / ERC1155 を選択して保有者情報を取得し、CSVとして出力。
        - Googleスプレッドシートにもログを記録。
        """

        # 1) 一旦エフェメラル(他ユーザー非表示)で処理中メッセージを返す
        await interaction.response.defer(ephemeral=True)

        # 2) APIのベース設定
        base_url = "https://api.socialscan.io/monad-testnet/v1/developer/api"
        module = "account"
        offset = 100
        page = 1

        # 3) ercの種類によってactionを切り替える
        if erc == "20":
            action = "addresstokenbalance"       # ERC20
        elif erc == "721":
            action = "addresstokennftbalance"    # ERC721
        elif erc == "1155":
            action = "addresstoken1155balance"   # ERC1155
        else:
            await interaction.followup.send(
                content="Invalid ERC type. Please specify '20', '721', or '1155'.",
                ephemeral=True
            )
            return

        # 4) 辞書でアドレスごとの数量を集計
        address_to_quantity = {}

        # ページングしながら取得
        while True:
            params = {
                "module": module,
                "action": action,
                "address": contract_address,  # ※従来のcontractaddressパラメータではなくaddressに変更
                "page": page,
                "offset": offset,
                "apikey": API_KEY
            }
            response = requests.get(base_url, params=params)
            data = response.json()

            # status != "1" または resultが空 の場合は終了
            if data.get("status") != "1" or not data.get("result"):
                break

            result_list = data["result"]

            for holder in result_list:
                # TokenHolderQuantityを文字列で取得 (キーの存在を確実にするため getを利用)
                raw_quantity_str = holder.get("TokenHolderQuantity", "0")

                # ERC20なら decimals=18 を考慮して小数へ変換
                if erc == "20":
                    # まず int にパースし、1e18で割ってfloat化
                    raw_int = int(raw_quantity_str)
                    quantity = raw_int / (10**18)
                else:
                    # ERC721 / ERC1155 => 小数点なし
                    quantity = int(raw_quantity_str)

                # アドレスをキーに数量を累積
                address = holder["TokenHolderAddress"]
                if address not in address_to_quantity:
                    address_to_quantity[address] = quantity
                else:
                    address_to_quantity[address] += quantity

            # 次のページへ
            if len(result_list) < offset:
                break
            page += 1
            # time.sleep(0.2) は削除（Delay不要）

        # 5) 結果の集計
        total_holders = len(address_to_quantity)
        total_supply_value = sum(address_to_quantity.values())

        # 表示用のSupply文字列をフォーマット（ERC20は小数表示の可能性、他は整数）
        if erc == "20":
            # 小数を含む可能性あり => is_integer()なら整数表示
            if total_supply_value.is_integer():
                total_supply_str = str(int(total_supply_value))
            else:
                total_supply_str = str(total_supply_value)
        else:
            # 721/1155 は小数点なし
            total_supply_str = str(int(total_supply_value))

        # 6) CSVの作成（インメモリ）
        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["TokenHolderAddress", "TokenHolderQuantity"])

        # ERC20の場合、1件ずつも同様に整数 or 小数の判定
        for address, qty in address_to_quantity.items():
            if erc == "20":
                if qty.is_integer():
                    qty_str = str(int(qty))
                else:
                    qty_str = str(qty)
            else:
                qty_str = str(int(qty))
            writer.writerow([address, qty_str])

        csv_buffer.seek(0)
        file_to_send = discord.File(fp=csv_buffer, filename="holderList.csv")

        # 7) Discord表示用メッセージ
        summary_text = (
            f"**Contract Address**: {contract_address}\n"
            f"**Total Holders**: {total_holders}\n"
            f"**Total Supply**: {total_supply_str}\n"
            f"**ERC Type**: {erc}\n\n"
            "Your CSV file is attached below."
        )

        # 8) Googleシートへログ書き込み
        #    指示により「ログでは supply の小数点を削除」する
        user_name = str(interaction.user)
        log_supply_str = str(int(total_supply_value))  # 小数点部分は切り捨て

        # ログ形式: [Discord name, contract, holders, supply, ERC]
        worksheet.append_row(
            [user_name, contract_address, str(total_holders), log_supply_str, erc],
            value_input_option="RAW"
        )

        # 9) 実際に回答を返す
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
