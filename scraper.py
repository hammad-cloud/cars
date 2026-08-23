import asyncio
import os
from pathlib import Path
from urllib.parse import urljoin

import requests
from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)


# ============================================================
# CONFIGURATION
# ============================================================

BASE_URL = os.getenv(
    "AUTOWEB_BASE_URL",
    "https://autowebdirect.com/",
)

STOCK_URL = os.getenv(
    "AUTOWEB_STOCK_URL",
    "https://autowebdirect.com/stock",
)

AUTOWEB_USER = os.getenv("AUTOWEB_USER")
AUTOWEB_PASS = os.getenv("AUTOWEB_PASS")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"

DEBUG_DIR = Path("playwright-debug")
DEBUG_DIR.mkdir(exist_ok=True)


# ============================================================
# DISCORD
# ============================================================

def send_discord_alert(message: str) -> None:
    """Send a message to Discord."""
    if not DISCORD_WEBHOOK_URL:
        print("[!] DISCORD_WEBHOOK_URL is not configured.")
        return

    try:
        response = requests.post(
            DISCORD_WEBHOOK_URL,
            json={"content": message},
            timeout=15,
        )

        if response.status_code in (200, 204):
            print("[+] Discord notification sent.")
        else:
            print(
                f"[!] Discord notification failed: "
                f"{response.status_code} - {response.text[:500]}"
            )

    except requests.RequestException as exc:
        print(f"[!] Discord request failed: {exc}")


# ============================================================
# DEBUGGING
# ============================================================

async def save_debug_files(page, name: str = "failure") -> None:
    """
    Save screenshot and HTML when something goes wrong.
    These files are extremely useful in GitHub Actions.
    """
    try:
        screenshot_path = DEBUG_DIR / f"{name}.png"
        html_path = DEBUG_DIR / f"{name}.html"

        await page.screenshot(
            path=str(screenshot_path),
            full_page=True,
        )

        html = await page.content()
        html_path.write_text(html, encoding="utf-8")

        print(f"[+] Debug screenshot: {screenshot_path}")
        print(f"[+] Debug HTML: {html_path}")

    except Exception as exc:
        print(f"[!] Could not save debug files: {exc}")


async def print_page_debug_info(page) -> None:
    """Print useful information about the current page."""
    print()
    print("========== PAGE DEBUG ==========")
    print(f"URL:   {page.url}")

    try:
        print(f"TITLE: {await page.title()}")
    except Exception:
        pass

    try:
        inputs = page.locator("input")
        count = await inputs.count()

        print(f"INPUT COUNT: {count}")

        for i in range(count):
            element = inputs.nth(i)

            try:
                input_type = await element.get_attribute("type")
                name = await element.get_attribute("name")
                element_id = await element.get_attribute("id")
                placeholder = await element.get_attribute("placeholder")
                value = await element.get_attribute("value")

                print(
                    f"INPUT {i}: "
                    f"type={input_type!r}, "
                    f"name={name!r}, "
                    f"id={element_id!r}, "
                    f"placeholder={placeholder!r}, "
                    f"value={value!r}"
                )
            except Exception:
                pass

    except Exception as exc:
        print(f"[!] Could not inspect inputs: {exc}")

    print("================================")
    print()


# ============================================================
# LOGIN
# ============================================================

async def find_username_input(page):
    """
    Find the User ID field.

    AutoWeb Direct currently displays a generic User ID input,
    so we cannot rely only on input[type=email] or name=username.
    """

    # First try conventional selectors.
    selectors = [
        "input[name='username']",
        "input[name='user']",
        "input[name='userid']",
        "input[id='username']",
        "input[id='userid']",
        "input[placeholder*='User' i]",
        "input[placeholder*='ID' i]",
        "input[type='email']",
    ]

    for selector in selectors:
        locator = page.locator(selector).first

        try:
            if await locator.count() > 0 and await locator.is_visible():
                print(f"[+] Username selector found: {selector}")
                return locator
        except Exception:
            continue

    # Fallback:
    # Find the first visible text-like input that is NOT the password.
    inputs = page.locator(
        "input:not([type='hidden']):not([type='password']):"
        "not([type='checkbox']):not([type='radio']):"
        "not([type='submit']):not([type='button']):"
        "not([type='image'])"
    )

    count = await inputs.count()

    for i in range(count):
        locator = inputs.nth(i)

        try:
            if await locator.is_visible():
                print(f"[+] Username field found using generic input #{i}")
                return locator
        except Exception:
            continue

    return None


async def find_password_input(page):
    """Find the password field."""

    selectors = [
        "input[type='password']",
        "input[name='password']",
        "input[id='password']",
        "input[placeholder*='Password' i]",
    ]

    for selector in selectors:
        locator = page.locator(selector).first

        try:
            if await locator.count() > 0 and await locator.is_visible():
                print(f"[+] Password selector found: {selector}")
                return locator
        except Exception:
            continue

    return None


async def find_login_button(page):
    """Find the login/submit control."""

    selectors = [
        "button[type='submit']",
        "input[type='submit']",
        "input[type='image']",
        "button:has-text('Login')",
        "button:has-text('Log In')",
        "button:has-text('Sign In')",
        "input[value*='Login' i]",
        "input[value*='Log In' i]",
        "input[value*='Sign In' i]",
    ]

    for selector in selectors:
        locator = page.locator(selector).first

        try:
            if await locator.count() > 0 and await locator.is_visible():
                print(f"[+] Login button found: {selector}")
                return locator
        except Exception:
            continue

    # Last-resort fallback: find a form containing a password input
    # and use its submit/input control.
    password = page.locator("input[type='password']").first

    if await password.count() > 0:
        try:
            form = password.locator("xpath=ancestor::form[1]")

            if await form.count() > 0:
                submit = form.locator(
                    "button, input[type='submit'], input[type='image']"
                ).first

                if await submit.count() > 0 and await submit.is_visible():
                    print("[+] Login button found inside login form.")
                    return submit
        except Exception:
            pass

    return None


async def login(page) -> bool:
    """Log into AutoWeb Direct."""

    print("[*] Navigating to AutoWeb Direct...")

    await page.goto(
        BASE_URL,
        wait_until="domcontentloaded",
        timeout=60000,
    )

    # Give the site's JavaScript a short chance to initialize.
    await page.wait_for_timeout(1500)

    print(f"[*] Current URL: {page.url}")

    # The live page has a User ID + Password form.
    print("[*] Locating User ID field...")

    username_input = await find_username_input(page)

    if username_input is None:
        print("[!] Could not find User ID field.")
        await print_page_debug_info(page)
        await save_debug_files(page, "login_username_not_found")
        return False

    print("[*] Filling User ID...")
    await username_input.fill(AUTOWEB_USER)

    print("[*] Locating password field...")

    password_input = await find_password_input(page)

    if password_input is None:
        print("[!] Could not find password field.")
        await print_page_debug_info(page)
        await save_debug_files(page, "login_password_not_found")
        return False

    print("[*] Filling password...")
    await password_input.fill(AUTOWEB_PASS)

    print("[*] Locating login button...")

    login_button = await find_login_button(page)

    if login_button is None:
        print("[!] Could not find login button.")
        await print_page_debug_info(page)
        await save_debug_files(page, "login_button_not_found")
        return False

    print("[*] Submitting login...")

    old_url = page.url

    try:
        async with page.expect_navigation(
            wait_until="domcontentloaded",
            timeout=15000,
        ):
            await login_button.click()

    except PlaywrightTimeoutError:
        # Some older sites submit/login using JavaScript without
        # triggering a normal navigation. That is not automatically
        # a failure.
        print("[!] No normal navigation detected after login click.")

        try:
            await login_button.click(timeout=5000)
        except Exception:
            pass

    await page.wait_for_timeout(2500)

    print(f"[*] URL after login: {page.url}")

    # Check for obvious login errors.
    body_text = ""

    try:
        body_text = (await page.locator("body").inner_text()).lower()
    except Exception:
        pass

    login_error_words = [
        "incorrect password",
        "invalid password",
        "invalid username",
        "incorrect username",
        "login failed",
        "authentication failed",
        "user not found",
    ]

    for error_text in login_error_words:
        if error_text in body_text:
            print(f"[!] Login appears to have failed: {error_text}")
            await save_debug_files(page, "login_failed")
            return False

    # If URL changed, that's a useful sign.
    if page.url != old_url:
        print("[+] Login caused a navigation.")

    # Check whether the password field disappeared.
    password_still_visible = False

    try:
        password_still_visible = await page.locator(
            "input[type='password']"
        ).first.is_visible()
    except Exception:
        pass

    if password_still_visible:
        print(
            "[!] Password field is still visible. "
            "Login may not have succeeded."
        )

        await print_page_debug_info(page)
        await save_debug_files(page, "login_maybe_failed")

        return False

    print("[+] Login sequence completed.")
    return True


# ============================================================
# STOCK PAGE
# ============================================================

async def open_stock_page(page) -> bool:
    """Open the stock page after authentication."""

    print("[*] Opening stock page...")
    print(f"[*] Stock URL: {STOCK_URL}")

    try:
        response = await page.goto(
            STOCK_URL,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        await page.wait_for_timeout(2000)

        print(f"[*] Stock page URL: {page.url}")

        if response:
            print(f"[*] Stock HTTP status: {response.status}")

            if response.status >= 400:
                print(
                    f"[!] Stock page returned HTTP {response.status}"
                )

        # Detect if the site redirected us back to login.
        body_text = ""

        try:
            body_text = (await page.locator("body").inner_text()).lower()
        except Exception:
            pass

        if "login to your account" in body_text:
            print("[!] We appear to be back on the login page.")
            return False

        return True

    except Exception as exc:
        print(f"[!] Failed to open stock page: {exc}")
        await save_debug_files(page, "stock_navigation_failed")
        return False


# ============================================================
# SCRAPING
# ============================================================

async def scrape_mira_listings(page) -> int:
    """
    Find vehicle listings containing 'MIRA'.

    Multiple selectors are tried because the site's markup may change.
    """

    selectors = [
        ".car-card",
        ".vehicle-row",
        ".auction-item",
        ".vehicle",
        ".car",
        "tr",
        "article",
    ]

    cars = None

    for selector in selectors:
        locator = page.locator(selector)

        try:
            count = await locator.count()

            if count > 0:
                print(
                    f"[+] Found {count} elements using {selector}"
                )
                cars = locator
                break

        except Exception:
            continue

    if cars is None:
        print("[!] No known vehicle containers found.")

        # Fallback: inspect the entire page for MIRA.
        body_text = await page.locator("body").inner_text()

        if "MIRA" in body_text.upper():
            print(
                "[!] 'MIRA' exists on the page, "
                "but no vehicle container selector matched."
            )
            await save_debug_files(page, "mira_container_not_found")

        return 0

    found_count = 0
    seen_links = set()

    count = await cars.count()

    for i in range(count):
        car = cars.nth(i)

        try:
            text = (await car.inner_text()).strip()

            if not text:
                continue

            if "MIRA" not in text.upper():
                continue

            found_count += 1

            # Find a link inside the listing.
            link = ""

            links = car.locator("a")
            link_count = await links.count()

            if link_count > 0:
                href = await links.first.get_attribute("href")

                if href:
                    link = urljoin(page.url, href)

            # Avoid duplicate Discord alerts.
            unique_key = f"{text[:300]}|{link}"

            if unique_key in seen_links:
                continue

            seen_links.add(unique_key)

            details = " ".join(text.split())

            if len(details) > 500:
                details = details[:500] + "..."

            alert_msg = (
                "🚗 **Mira Found!**\n"
                f"**Details:** {details}\n"
            )

            if link:
                alert_msg += f"**Link:** {link}"

            print()
            print("[+] MIRA LISTING FOUND")
            print(details)
            print(f"Link: {link}")
            print()

            send_discord_alert(alert_msg)

        except Exception as exc:
            print(f"[!] Error processing listing #{i}: {exc}")

    return found_count


# ============================================================
# MAIN
# ============================================================

async def main() -> None:
    print("========================================")
    print(" AutoWeb Direct Mira Scraper")
    print("========================================")

    # Validate environment variables.
    if not AUTOWEB_USER:
        print("[!] AUTOWEB_USER is missing.")
        return

    if not AUTOWEB_PASS:
        print("[!] AUTOWEB_PASS is missing.")
        return

    if not DISCORD_WEBHOOK_URL:
        print(
            "[!] Warning: DISCORD_WEBHOOK_URL is missing. "
            "Scraping will continue without Discord alerts."
        )

    async with async_playwright() as p:

        browser = None

        try:
            print("[*] Starting Chromium...")

            browser = await p.chromium.launch(
                headless=HEADLESS,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )

            context = await browser.new_context(
                viewport={
                    "width": 1440,
                    "height": 900,
                },
                locale="en-US",
                timezone_id="Asia/Tokyo",
            )

            page = await context.new_page()

            # Reasonable default timeout.
            page.set_default_timeout(30000)
            page.set_default_navigation_timeout(60000)

            # Log useful browser errors.
            page.on(
                "console",
                lambda msg: print(
                    f"[browser:{msg.type}] {msg.text}"
                ),
            )

            page.on(
                "pageerror",
                lambda exc: print(
                    f"[browser page error] {exc}"
                ),
            )

            # ------------------------------------------------
            # LOGIN
            # ------------------------------------------------

            logged_in = await login(page)

            if not logged_in:
                print("[!] Login failed.")
                send_discord_alert(
                    "⚠️ AutoWeb scraper: login failed. "
                    "Check GitHub Actions debug artifacts."
                )
                return

            # ------------------------------------------------
            # STOCK
            # ------------------------------------------------

            stock_opened = await open_stock_page(page)

            if not stock_opened:
                print("[!] Could not open authenticated stock page.")

                await print_page_debug_info(page)
                await save_debug_files(
                    page,
                    "stock_page_failed",
                )

                send_discord_alert(
                    "⚠️ AutoWeb scraper: login succeeded or partially "
                    "succeeded, but the stock page could not be opened."
                )

                return

            # ------------------------------------------------
            # SCRAPE
            # ------------------------------------------------

            print("[*] Searching for MIRA listings...")

            found_count = await scrape_mira_listings(page)

            if found_count == 0:
                print("[*] No MIRA listings found.")
                send_discord_alert(
                    "ℹ️ Daily AutoWeb scan finished: "
                    "No new MIRA listings found."
                )
            else:
                print(
                    f"[+] Scan complete. "
                    f"Found {found_count} MIRA listing(s)."
                )

        except Exception as exc:
            print()
            print("========================================")
            print("[!] SCRAPER FAILED")
            print("========================================")
            print(f"{type(exc).__name__}: {exc}")
            print()

            try:
                await print_page_debug_info(page)
                await save_debug_files(page, "unexpected_failure")
            except Exception:
                pass

            send_discord_alert(
                "🚨 AutoWeb scraper crashed.\n"
                f"Error: {type(exc).__name__}: {str(exc)[:500]}"
            )

            raise

        finally:
            if browser:
                print("[*] Closing browser...")
                await browser.close()

    print("[+] Scraper finished.")


if __name__ == "__main__":
    asyncio.run(main())
