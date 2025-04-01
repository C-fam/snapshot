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

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
API_KEY = os.getenv("API_KEY")
SERVICE_ACCOUNT_INFO = os.getenv("SERVICE_ACCOUNT_INFO")

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
        description="Fetch token holder info. ERC20/721/1155 selectable"
    )
    @app_commands.describe(
        erc="Choose ERC type (20/721/1155)",
        contract_address="Enter the contract address"
    )
    @app_commands.choices(
        erc=[
            app_commands.Choice(name="ERC20 (fungible, decimals=18)", value="20"),
            app_commands.Choice(name="ERC721 (NFT)", value="721"),
            app_commands.Choice(name="ERC1155 (multi-token)", value="1155"),
        ]
    )
    async def snapshot(
        self,
        interaction: discord.Interaction,
        erc: app_commands.Choice[str],
        contract_address: str
    ):
        """
        /snapshot erc:<20|721|1155> contract_address:<0x...>

        ※ 各ERC規格の簡潔解説:
        - ERC20: 同一単位で交換可能なFungible Token(例: 通貨)で、小数点(18桁)を扱う
        - ERC721: Non-Fungible Token。1つ1つが固有のIDをもつNFT規格
        - ERC1155: FungibleとNon-Fungible両方を扱えるマルチトークン規格
        """
        await interaction.response.defer(ephemeral=True)

        base_url = "https://api.socialscan.io/monad-testnet/v1/developer/api"
        module = "account"
        offset = 100
        page = 1

        # ercの選択値に基づいてactionを切り替え
        if erc.value == "20":
            action = "addresstokenbalance"      # ERC20
        elif erc.value == "721":
            action = "addresstokennftbalance"   # ERC721
        elif erc.value == "1155":
            action = "addresstoken1155balance"  # ERC1155
        else:
            await interaction.followup.send(
                content="Invalid ERC type. Please choose from 20, 721, or 1155.",
                ephemeral=True
            )
            return

        address_to_quantity = {}

        while True:
            params = {
                "module": module,
                "action": action,
                "address": contract_address,
                "page": page,
                "offset": offset,
                "apikey": API_KEY
            }
            response = requests.get(base_url, params=params)
            data = response.json()

            if data.get("status") != "1" or not data.get("result"):
                # APIがデータを返さなかった場合は終了
                break

            result_list = data["result"]

            for holder in result_list:
                raw_quantity_str = holder.get("TokenHolderQuantity", "0")

                if erc.value == "20":
                    # ERC20 は decimals=18 を考慮して数値変換
                    raw_int = int(raw_quantity_str)
                    quantity = raw_int / (10**18)
                else:
                    # ERC721 / ERC1155 → 小数点考慮なし
                    quantity = int(raw_quantity_str)

                address = holder["TokenHolderAddress"]
                if address not in address_to_quantity:
                    address_to_quantity[address] = quantity
                else:
                    address_to_quantity[address] += quantity

            if len(result_list) < offset:
                # 取得件数がoffsetに満たない → 最終ページ
                break
            page += 1

        # 集計
        total_holders = len(address_to_quantity)
        total_supply_val = sum(address_to_quantity.values())

        # 表示用のトータルサプライ文字列
        if erc.value == "20":
            # ERC20 → 小数点があれば保持
            if total_supply_val.is_integer():
                total_supply_str = str(int(total_supply_val))
            else:
                total_supply_str = str(total_supply_val)
        else:
            total_supply_str = str(int(total_supply_val))

        # CSV出力 (in-memory)
        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["TokenHolderAddress", "TokenHolderQuantity"])

        for address, qty in address_to_quantity.items():
            if erc.value == "20":
                # 小数点保持 or 切り捨て
                if qty.is_integer():
                    qty_str = str(int(qty))
                else:
                    qty_str = str(qty)
            else:
                qty_str = str(int(qty))

            writer.writerow([address, qty_str])

        csv_buffer.seek(0)
        file_to_send = discord.File(fp=csv_buffer, filename="holderList.csv")

        # Discordへ送るメッセージ
        summary_text = (
            f"**Contract Address**: {contract_address}\n"
            f"**Total Holders**: {total_holders}\n"
            f"**Total Supply**: {total_supply_str}\n"
            f"**ERC Type**: {erc.value}\n\n"
            "Your CSV file is attached below."
        )

        # Googleシートへのログ。小数点は消す（整数化）
        supply_for_log = str(int(total_supply_val))
        user_name = str(interaction.user)

        worksheet.append_row(
            [user_name, contract_address, str(total_holders), supply_for_log, erc.value],
            value_input_option="RAW"
        )

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
