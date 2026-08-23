import asyncio
import os
import requests
from playwright.async_api import async_playwright

# Fetch secrets from environment variables
AUTOWEB_USER = os.getenv("AUTOWEB_USER")
AUTOWEB_PASS = os.getenv("AUTOWEB_PASS")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

def send_discord_alert(message):
    """Sends a text notification to the configured Discord channel."""
    if not DISCORD_WEBHOOK_URL:
        print("[!] DISCORD_WEBHOOK_URL environment variable is not set.")
        return
    
    payload = {"content": message}
    response = requests.post(DISCORD_WEBHOOK_URL, json=payload)
    if response.status_code == 204 or response.status_code == 200:
        print("[+] Discord notification sent successfully.")
    else:
        print(f"[!] Failed to send Discord notification: {response.status_code} - {response.text}")

async def main():
    if not AUTOWEB_USER or not AUTOWEB_PASS:
        print("[!] Missing login credentials in environment variables.")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        print("[*] Navigating to target site...")
        await page.goto("https://autowebdirect.com", wait_until="domcontentloaded")

        # Flexible selector handling to prevent locator timeouts
        print("[*] Locating username/email input field...")
        username_selector = "input[type='email'], input[name='username'], input[name='email'], input[name='login'], #username, #email"
        await page.wait_for_selector(username_selector, timeout=20000)
        username_input = page.locator(username_selector).first
        await username_input.fill(AUTOWEB_USER)

        print("[*] Locating password input field...")
        password_selector = "input[type='password'], input[name='password'], #password"
        await page.wait_for_selector(password_selector, timeout=20000)
        password_input = page.locator(password_selector).first
        await password_input.fill(AUTOWEB_PASS)

        # Submit form
        print("[*] Submitting login credentials...")
        submit_button = page.locator("button[type='submit'], input[type='submit'], button:has-text('Login'), button:has-text('Sign In')").first
        await submit_button.click()

        # Wait for navigation or post-login state
        await page.wait_for_load_state("networkidle")
        print("[+] Login sequence executed.")

        # --- SCRAPING & ALERT LOGIC HERE ---
        print("[*] Navigating to stock page...")
        await page.goto("https://autowebdirect.com/stock", wait_until="networkidle")

        cars = await page.locator(".car-card, .vehicle-row, .auction-item").all()
        found_count = 0

        for car in cars:
            text = await car.inner_text()
            if "MIRA" in text.upper():
                found_count += 1
                link_element = car.locator("a").first
                link = await link_element.get_attribute("href") if await link_element.count() > 0 else ""
                
                alert_msg = f"🚗 **Mira Found!**\nDetails: {text.strip()[:150]}...\nLink: https://autowebdirect.com{link}"
                send_discord_alert(alert_msg)

        if found_count == 0:
            print("[*] No new Mira listings detected.")
            send_discord_alert("ℹ️ Daily scan finished: No new Mira listings found today.")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
