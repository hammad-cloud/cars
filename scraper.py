import os
import asyncio
import requests
from playwright.async_api import async_playwright

AUTOWEB_USER = os.environ.get("AUTOWEB_USER")
AUTOWEB_PASS = os.environ.get("AUTOWEB_PASS")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

TARGET_GRADES = ["4", "4.5", "5"]
TARGET_COLORS = ["W19", "W25", "S28", "WHITE", "PEARL", "SILVER"]
BACK_CAM_KEYWORDS = ["バック", "Bカメラ", "バックカメラ", "NAVI READY", "ナビ済", "BACK CAM"]

def send_discord_alert(car):
    embed = {
        "title": "🚨 Matched Daihatsu Mira Found!",
        "description": "A new vehicle matching your specs was listed on AutoWeb Direct.",
        "color": 3066993,  # Green
        "fields": [
            {"name": "📌 Lot Number", "value": str(car['lot']), "inline": True},
            {"name": "🏛️ Auction House", "value": str(car['auction']), "inline": True},
            {"name": "📅 Year", "value": str(car['year']), "inline": True},
            {"name": "🛣️ Mileage", "value": str(car['mileage']), "inline": True},
            {"name": "🎨 Color Code", "value": str(car['color']), "inline": True},
            {"name": "⭐ Auction Grade", "value": f"Grade {car['grade']}", "inline": True},
            {"name": "📷 Camera Status", "value": "Factory Back Camera Confirmed", "inline": False},
            {"name": "🔗 Auction Link", "value": f"[View Lot Details]({car['url']})", "inline": False}
        ],
        "footer": {"text": "AutoWeb Direct Automated Scanner • Daily 11 PM PKT Run"}
    }
    
    payload = {
        "username": "Mira Auction Bot",
        "avatar_url": "https://i.imgur.com/4M34hi2.png",
        "embeds": [embed]
    }
    
    requests.post(DISCORD_WEBHOOK_URL, json=payload)

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        # 1. Login to AutoWeb Direct
        await page.goto("https://autowebdirect.com/spn/")
        await page.fill("input[name='login']", AUTOWEB_USER)
        await page.fill("input[name='password']", AUTOWEB_PASS)
        await page.click("input[type='submit']")
        await page.wait_for_load_state("networkidle")

        # 2. Search Japan Auctions Frame
        await page.goto("https://auc.autowebdirect.com/japan")
        await page.select_option("select#make", label="DAIHATSU")
        await page.select_option("select#model", label="MIRA E S")
        await page.select_option("select#year_from", value="2022")
        await page.select_option("select#year_to", value="2025")
        await page.fill("input#mileage_from", "2000")
        await page.fill("input#mileage_to", "20000")
        
        await page.click("#search_btn")
        await page.wait_for_selector(".car_table_row", timeout=15000)

        # 3. Filter Results
        rows = await page.query_selector_all(".car_table_row")
        for row in rows:
            color = (await (await row.query_selector(".col_color")).inner_text()).upper()
            grade = await (await row.query_selector(".col_grade")).inner_text()
            
            if grade in TARGET_GRADES and any(c in color for c in TARGET_COLORS):
                lot_no = await (await row.query_selector(".col_lot")).inner_text()
                auction = await (await row.query_selector(".col_auction")).inner_text()
                
                # Check Auction Sheet Details
                detail_page = await context.new_page()
                await detail_page.goto(f"https://autowebdirect.com/spn/lot_detail?lot={lot_no}")
                sheet_text = await detail_page.inner_text("body")
                
                if any(kw in sheet_text.upper() for kw in BACK_CAM_KEYWORDS):
                    send_discord_alert({
                        "lot": lot_no,
                        "auction": auction,
                        "year": await (await row.query_selector(".col_year")).inner_text(),
                        "mileage": await (await row.query_selector(".col_mileage")).inner_text(),
                        "color": color,
                        "grade": grade,
                        "url": detail_page.url
                    })
                await detail_page.close()

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
