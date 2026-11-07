import os
import asyncio
import aiohttp
from dotenv import load_dotenv

load_dotenv()
API_URL = "https://bantibhaiya.to/api/reseller_v1.php"
API_KEY = os.getenv("API_KEY")
MASTER_KEY = os.getenv("MASTER_KEY")

ACTIONS = ["products", "product_list", "get_products", "list_products",
           "list", "balance", "profile", "info"]

async def main():
    headers = {
        "x-master-key": MASTER_KEY,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    async with aiohttp.ClientSession() as session:
        for action in ACTIONS:
            data = {"api_key": API_KEY, "action": action}
            try:
                async with session.post(API_URL, data=data, headers=headers,
                                        timeout=aiohttp.ClientTimeout(total=20)) as r:
                    text = await r.text()
                    print(f"--- {action} | HTTP {r.status}")
                    print(text[:600])
            except Exception as ex:
                print(f"--- {action} | қате: {ex}")

asyncio.run(main())