import asyncio
import json
import os
import re
from pathlib import Path
from urllib.parse import urljoin

import requests
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


# ============================================================
# CONFIGURATION
# ============================================================

BASE_URL = os.getenv(
    "AUCTION_URL",
    "https://auc.alubaidmotors.com/japan",
)

USERNAME = os.getenv("USERNAME")
PASSWORD = os.getenv("PASSWORD")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"

# The user specifically requested at least one minute after
# pressing GET IMAGES.
IMAGE_WAIT_SECONDS = int(os.getenv("IMAGE_WAIT_SECONDS", "60"))
IMAGE_RETRIES = int(os.getenv("IMAGE_RETRIES", "2"))

DEBUG_DIR = Path("playwright-debug")
DEBUG_DIR.mkdir(exist_ok=True)

STATE_FILE = Path("mira_seen.json")

MIRA_RE = re.compile(r"\b(?:DAIHATSU\s+)?MIRA\b", re.I)
GET_IMAGES_RE = re.compile(r"GET\s+IMAGES?", re.I)

LOGIN_ERROR_WORDS = [
    "incorrect password",
    "invalid password",
    "invalid username",
    "incorrect username",
    "login failed",
    "authentication failed",
    "user not found",
]


# ============================================================
# DISCORD
# ============================================================

def send_discord_alert(message: str) -> bool:
    if not DISCORD_WEBHOOK_URL:
        print("[!] DISCORD_WEBHOOK_URL is not configured.")
        return False

    try:
        response = requests.post(
            DISCORD_WEBHOOK_URL,
            json={"content": message},
            timeout=20,
        )

        if response.status_code in (200, 204):
            print("[+] Discord notification sent.")
            return True

        print(
            f"[!] Discord notification failed: "
            f"{response.status_code} - {response.text[:500]}"
        )
        return False

    except requests.RequestException as exc:
        print(f"[!] Discord request failed: {exc}")
        return False


# ============================================================
# DEBUGGING
# ============================================================

async def save_debug_files(page, name: str) -> None:
    try:
        await page.screenshot(
            path=str(DEBUG_DIR / f"{name}.png"),
            full_page=True,
        )
        (DEBUG_DIR / f"{name}.html").write_text(
            await page.content(),
            encoding="utf-8",
        )
        print(f"[+] Debug files saved: {name}")
    except Exception as exc:
        print(f"[!] Could not save debug files: {exc}")


async def print_page_debug_info(page) -> None:
    print("\n========== PAGE DEBUG ==========")
    print(f"URL: {page.url}")
    try:
        print(f"TITLE: {await page.title()}")
    except Exception:
        pass

    try:
        body = await page.locator("body").inner_text()
        print(f"BODY TEXT LENGTH: {len(body)}")
        print(body[:5000])
    except Exception as exc:
        print(f"[!] Could not read body: {exc}")

    try:
        buttons = page.locator("button, input[type='button'], input[type='submit'], a")
        count = min(await buttons.count(), 100)
        print(f"VISIBLE CONTROLS TO INSPECT: {count}")

        for i in range(count):
            el = buttons.nth(i)
            try:
                if not await el.is_visible():
                    continue
                text = (await el.inner_text()).strip()
                value = await el.get_attribute("value")
                aria = await el.get_attribute("aria-label")
                href = await el.get_attribute("href")
                if text or value or aria or href:
                    print(
                        f"CONTROL {i}: text={text!r} value={value!r} "
                        f"aria={aria!r} href={href!r}"
                    )
            except Exception:
                continue
    except Exception as exc:
        print(f"[!] Could not inspect controls: {exc}")

    print("================================\n")


# ============================================================
# LOGIN
# ============================================================

async def find_username_input(page):
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
        try:
            loc = page.locator(selector).first
            if await loc.count() and await loc.is_visible():
                return loc
        except Exception:
            pass

    inputs = page.locator(
        "input:not([type='hidden']):not([type='password']):"
        "not([type='checkbox']):not([type='radio']):"
        "not([type='submit']):not([type='button']):"
        "not([type='image'])"
    )

    for i in range(await inputs.count()):
        loc = inputs.nth(i)
        try:
            if await loc.is_visible():
                return loc
        except Exception:
            pass

    return None


async def find_password_input(page):
    for selector in [
        "input[type='password']",
        "input[name='password']",
        "input[id='password']",
        "input[placeholder*='Password' i]",
    ]:
        try:
            loc = page.locator(selector).first
            if await loc.count() and await loc.is_visible():
                return loc
        except Exception:
            pass
    return None


async def find_login_button(page):
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
        try:
            loc = page.locator(selector).first
            if await loc.count() and await loc.is_visible():
                return loc
        except Exception:
            pass

    password = page.locator("input[type='password']").first
    try:
        if await password.count():
            form = password.locator("xpath=ancestor::form[1]")
            if await form.count():
                submit = form.locator(
                    "button, input[type='submit'], input[type='image']"
                ).first
                if await submit.count() and await submit.is_visible():
                    return submit
    except Exception:
        pass

    return None


async def login_if_needed(page) -> bool:
    print(f"[*] Opening auction website: {BASE_URL}")

    try:
        response = await page.goto(
            BASE_URL,
            wait_until="domcontentloaded",
            timeout=60000,
        )
        await page.wait_for_timeout(2500)
        print(f"[*] Current URL: {page.url}")
        if response:
            print(f"[*] HTTP status: {response.status}")
    except Exception as exc:
        print(f"[!] Could not open auction site: {exc}")
        await save_debug_files(page, "site_open_failed")
        return False

    # If the page is already authenticated, continue without login.
    try:
        body = (await page.locator("body").inner_text()).lower()
    except Exception:
        body = ""

    if not any(word in body for word in ("login", "user id", "password")):
        print("[+] Page appears to be already authenticated.")
        return True

    if not USERNAME or not PASSWORD:
        print(
            "[!] Login appears to be required, but AUTOWEB_USER/"
            "AUTOWEB_PASS are missing."
        )
        await save_debug_files(page, "login_required")
        return False

    username_input = await find_username_input(page)
    password_input = await find_password_input(page)
    login_button = await find_login_button(page)

    if not username_input or not password_input or not login_button:
        print("[!] Could not identify the login form.")
        await print_page_debug_info(page)
        await save_debug_files(page, "login_form_not_found")
        return False

    await username_input.fill(USERNAME)
    await password_input.fill(PASSWORD)

    print("[*] Submitting login...")
    try:
        await login_button.click()
    except Exception as exc:
        print(f"[!] Login click failed: {exc}")
        await save_debug_files(page, "login_click_failed")
        return False

    await page.wait_for_timeout(4000)

    try:
        body = (await page.locator("body").inner_text()).lower()
    except Exception:
        body = ""

    for word in LOGIN_ERROR_WORDS:
        if word in body:
            print(f"[!] Login failed: {word}")
            await save_debug_files(page, "login_failed")
            return False

    try:
        if await page.locator("input[type='password']").first.is_visible():
            print("[!] Password field is still visible; login may have failed.")
            await save_debug_files(page, "login_maybe_failed")
            return False
    except Exception:
        pass

    print("[+] Login completed.")
    return True


# ============================================================
# MIRA DISCOVERY
# ============================================================

async def all_pages(context):
    """Return pages and their frames so an iframe-based auction page is covered."""
    pages = list(context.pages)
    frames = []
    for p in pages:
        frames.extend(p.frames)
    return pages, frames


async def get_body_text(frame) -> str:
    try:
        return await frame.locator("body").inner_text(timeout=10000)
    except Exception:
        return ""


async def find_mira_candidates(frame):
    """
    Find likely auction listing containers without assuming one specific
    CSS class. We use MIRA text, then walk up the DOM until a useful
    container/link is found.
    """
    candidates = []
    seen = set()

    try:
        matches = frame.get_by_text(MIRA_RE)
        count = await matches.count()
    except Exception:
        count = 0
        matches = None

    print(f"[*] MIRA text matches in frame: {count}")

    for i in range(min(count, 300)):
        try:
            match = matches.nth(i)
            if not await match.is_visible():
                continue

            # Try several ancestor levels. The first useful ancestor is
            # normally the auction row/card/table row.
            ancestor = match
            for level in range(1, 9):
                ancestor = ancestor.locator("xpath=..")

                if not await ancestor.count():
                    break

                text = " ".join((await ancestor.inner_text()).split())
                if not text or len(text) < 20:
                    continue

                # Prefer an ancestor containing a listing link or
                # auction-specific information.
                link_count = await ancestor.locator("a[href]").count()
                if link_count or len(text) >= 80 or level >= 4:
                    try:
                        href = ""
                        if link_count:
                            href = await ancestor.locator("a[href]").first.get_attribute(
                                "href"
                            ) or ""

                        absolute = urljoin(frame.url, href) if href else ""

                        # Keep the candidate bounded; huge ancestors are
                        # page-level containers, not individual cars.
                        if len(text) <= 2000:
                            key = f"{absolute}|{text[:500]}"
                            if key not in seen:
                                seen.add(key)
                                candidates.append(
                                    {
                                        "locator": ancestor,
                                        "text": text,
                                        "href": absolute,
                                    }
                                )
                                break
                    except Exception:
                        pass

        except Exception as exc:
            print(f"[!] Candidate error #{i}: {exc}")

    return candidates


def looks_like_mira(text: str) -> bool:
    return bool(MIRA_RE.search(text or ""))


def normalize_details(text: str) -> str:
    text = " ".join((text or "").split())
    if len(text) > 1200:
        text = text[:1200] + "..."
    return text


# ============================================================
# GET IMAGES / IMAGE VERIFICATION
# ============================================================

async def count_loaded_images(scope) -> int:
    try:
        imgs = scope.locator("img")
        count = await imgs.count()
        loaded = 0

        for i in range(min(count, 300)):
            img = imgs.nth(i)
            try:
                if not await img.is_visible():
                    continue

                complete = await img.evaluate(
                    "(el) => el.complete && el.naturalWidth > 0"
                )
                src = await img.get_attribute("src")
                if complete and src:
                    loaded += 1
            except Exception:
                continue

        return loaded
    except Exception:
        return 0


async def find_get_images(scope):
    """
    Locate the green GET IMAGES control shown in the user's screenshot.
    We intentionally use several text/attribute strategies because the
    website may render it as a button, link, input, or JS control.
    """
    selectors = [
        "button",
        "a",
        "input[type='button']",
        "input[type='submit']",
        "[role='button']",
        "*",
    ]

    for selector in selectors:
        try:
            loc = scope.locator(selector)
            count = await loc.count()

            for i in range(min(count, 500)):
                el = loc.nth(i)
                try:
                    if not await el.is_visible():
                        continue

                    text = " ".join((await el.inner_text()).split())
                    value = await el.get_attribute("value") or ""
                    aria = await el.get_attribute("aria-label") or ""
                    title = await el.get_attribute("title") or ""

                    combined = f"{text} {value} {aria} {title}"

                    if GET_IMAGES_RE.search(combined):
                        return el
                except Exception:
                    continue
        except Exception:
            continue

    return None


async def wait_for_images(scope, minimum_images=1) -> int:
    deadline = asyncio.get_running_loop().time() + IMAGE_WAIT_SECONDS

    while asyncio.get_running_loop().time() < deadline:
        loaded = await count_loaded_images(scope)
        if loaded >= minimum_images:
            print(f"[+] Images loaded: {loaded}")
            return loaded

        await asyncio.sleep(5)

    loaded = await count_loaded_images(scope)
    print(f"[*] Image wait finished. Loaded images: {loaded}")
    return loaded


async def load_listing_images(scope, listing_number: int) -> tuple[int, bool]:
    """
    If GET IMAGES is visible, click it and wait at least 60 seconds.
    If it remains visible / images are still absent, retry.
    """
    loaded = await count_loaded_images(scope)

    if loaded > 0:
        print(
            f"[+] Listing #{listing_number}: images already loaded ({loaded})."
        )
        return loaded, True

    for attempt in range(1, IMAGE_RETRIES + 1):
        button = await find_get_images(scope)

        if button is None:
            # No button. Give the page a short chance to load images
            # without clicking anything.
            print(
                f"[*] Listing #{listing_number}: GET IMAGES button not visible."
            )
            loaded = await wait_for_images(scope, minimum_images=1)
            return loaded, loaded > 0

        print(
            f"[*] Listing #{listing_number}: clicking GET IMAGES "
            f"(attempt {attempt}/{IMAGE_RETRIES})..."
        )

        try:
            await button.scroll_into_view_if_needed()
            await button.click(timeout=15000)
        except Exception as exc:
            print(f"[!] GET IMAGES click failed: {exc}")
            await asyncio.sleep(3)
            continue

        # REQUIRED: at least one minute after pressing the button.
        print(
            f"[*] Waiting at least {IMAGE_WAIT_SECONDS} seconds for "
            "auction images..."
        )
        await asyncio.sleep(IMAGE_WAIT_SECONDS)

        loaded = await count_loaded_images(scope)
        print(f"[*] Images after attempt {attempt}: {loaded}")

        if loaded > 0:
            return loaded, True

    return loaded, loaded > 0


# ============================================================
# LISTING LINK / ID
# ============================================================

async def extract_listing_key(candidate) -> str:
    href = candidate.get("href", "")
    text = candidate.get("text", "")

    # Prefer a stable listing URL.
    if href:
        return href

    # Otherwise use strong auction identifiers from text.
    patterns = [
        r"\b(?:lot|lot\s*number)\s*[:#-]?\s*([A-Z0-9-]+)",
        r"\bchassis(?:\s*id|\s*no\.?)?\s*[:#-]?\s*([A-Z0-9-]+)",
        r"\b([A-Z]{1,5}\d{3,}[A-Z0-9-]*)\b",
    ]

    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if m:
            return m.group(1).upper()

    # Last-resort stable-ish hash.
    import hashlib
    return hashlib.sha256(text[:1000].encode("utf-8")).hexdigest()[:24]


# ============================================================
# STATE
# ============================================================

def load_seen() -> set[str]:
    if not STATE_FILE.exists():
        return set()

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return set(str(x) for x in data)
        if isinstance(data, dict) and isinstance(data.get("seen"), list):
            return set(str(x) for x in data["seen"])
    except Exception as exc:
        print(f"[!] Could not read state file: {exc}")

    return set()


def save_seen(seen: set[str]) -> None:
    # Keep the file bounded so it doesn't grow forever.
    values = sorted(seen)[-5000:]
    STATE_FILE.write_text(
        json.dumps(values, indent=2),
        encoding="utf-8",
    )


# ============================================================
# PROCESS MIRA LISTINGS
# ============================================================

async def process_candidates(page, frame, candidates, seen):
    verified_scan = True
    current_keys = set()
    new_count = 0

    for index, candidate in enumerate(candidates, start=1):
        try:
            text = candidate["text"]

            if not looks_like_mira(text):
                continue

            key = await extract_listing_key(candidate)
            current_keys.add(key)

            print("\n----------------------------------------")
            print(f"[+] MIRA candidate #{index}")
            print(f"KEY: {key}")
            print(f"URL: {candidate.get('href', '')}")
            print(f"DETAILS: {normalize_details(text)}")

            # If we already know this listing, do not send it again.
            # Still count it as a verified MIRA listing.
            if key in seen:
                print("[*] Already sent previously; skipping Discord alert.")
                continue

            listing_scope = candidate["locator"]

            # Try image loading within the listing first.
            image_count, image_ok = await load_listing_images(
                listing_scope,
                index,
            )

            # If the button/images live outside the small ancestor,
            # try the full frame as a fallback.
            if not image_ok:
                print(
                    "[*] Images not found inside candidate; checking page "
                    "scope for GET IMAGES."
                )
                page_button = await find_get_images(frame)
                if page_button is not None:
                    try:
                        await page_button.scroll_into_view_if_needed()
                        await page_button.click(timeout=15000)
                        print(
                            f"[*] Waiting {IMAGE_WAIT_SECONDS} seconds "
                            "after page-level GET IMAGES..."
                        )
                        await asyncio.sleep(IMAGE_WAIT_SECONDS)
                        image_count = await count_loaded_images(frame)
                        image_ok = image_count > 0
                    except Exception as exc:
                        print(f"[!] Page-level image load failed: {exc}")

            if not image_ok:
                verified_scan = False
                print(
                    f"[!] MIRA listing found but images could not be "
                    f"verified: {key}"
                )

            details = normalize_details(text)
            link = candidate.get("href", "")

            alert = (
                "🚗 **NEW DAIHATSU MIRA FOUND!**\n"
                f"**Details:** {details}\n"
                f"**Images loaded:** {image_count}\n"
            )

            if link:
                alert += f"**Auction link:** {link}\n"

            if not image_ok:
                alert += (
                    "⚠️ **Image warning:** GET IMAGES was attempted, "
                    "but no loaded auction image could be verified."
                )

            send_discord_alert(alert)

            # Mark as seen only after an alert was successfully attempted.
            seen.add(key)
            new_count += 1

        except Exception as exc:
            verified_scan = False
            print(f"[!] Error processing MIRA candidate: {exc}")

    return verified_scan, new_count, current_keys


# ============================================================
# MAIN SCAN
# ============================================================

async def run_scan() -> None:
    seen = load_seen()

    async with async_playwright() as p:
        browser = None

        try:
            print("========================================")
            print(" Al Ubaid Japan DAIHATSU MIRA Scanner")
            print("========================================")

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
                viewport={"width": 1440, "height": 900},
                locale="en-US",
                timezone_id="Asia/Tokyo",
            )

            page = await context.new_page()
            page.set_default_timeout(30000)
            page.set_default_navigation_timeout(60000)

            page.on(
                "console",
                lambda msg: print(f"[browser:{msg.type}] {msg.text}"),
            )
            page.on(
                "pageerror",
                lambda exc: print(f"[browser page error] {exc}"),
            )

            if not await login_if_needed(page):
                send_discord_alert(
                    "🚨 **MIRA scanner:** could not open/authenticate "
                    "the Al Ubaid Japan auction page. "
                    "This scan was NOT verified."
                )
                return

            # The auction site may use the main page or an iframe.
            pages, frames = await all_pages(context)

            all_candidates = []
            for frame in frames:
                print(f"[*] Scanning frame: {frame.url}")
                candidates = await find_mira_candidates(frame)
                all_candidates.extend((frame, c) for c in candidates)

            # Deduplicate candidates by URL/text.
            unique = []
            dedupe = set()

            for frame, candidate in all_candidates:
                key = (
                    candidate.get("href", ""),
                    candidate.get("text", "")[:500],
                )
                if key in dedupe:
                    continue
                dedupe.add(key)
                unique.append((frame, candidate))

            print(f"[*] Unique MIRA candidates: {len(unique)}")

            if not unique:
                # Before declaring zero, save diagnostics. This is critical
                # because the old scraper incorrectly treated selector
                # failure as "no cars".
                await print_page_debug_info(page)
                await save_debug_files(page, "mira_scan_no_candidates")

                # We cannot confidently call this a verified zero if the
                # page structure could not be discovered.
                send_discord_alert(
                    "⚠️ **MIRA scan could not be verified.**\n"
                    "The Al Ubaid Japan page was opened, but the scanner "
                    "could not identify any MIRA listing containers. "
                    "It did NOT report this as 'No new MIRA listing'.\n"
                    "Check the GitHub Actions debug screenshot/HTML."
                )
                return

            verified = True
            new_count = 0

            # Process each frame's candidates.
            for frame, candidate in unique:
                ok, added, _ = await process_candidates(
                    page,
                    frame,
                    [candidate],
                    seen,
                )
                verified = verified and ok
                new_count += added

            if verified:
                save_seen(seen)

            if new_count == 0 and verified:
                send_discord_alert(
                    "ℹ️ **Daily MIRA scan completed.**\n"
                    "No new MIRA listing found."
                )
            elif new_count > 0:
                save_seen(seen)
                print(f"[+] New MIRA listings sent to Discord: {new_count}")

            if not verified:
                send_discord_alert(
                    "⚠️ **MIRA scan completed with warnings.**\n"
                    "At least one MIRA listing was found, but image loading "
                    "could not be fully verified."
                )

        except Exception as exc:
            print("========================================")
            print("[!] SCANNER FAILED")
            print("========================================")
            print(f"{type(exc).__name__}: {exc}")

            try:
                await print_page_debug_info(page)
                await save_debug_files(page, "unexpected_failure")
            except Exception:
                pass

            send_discord_alert(
                "🚨 **MIRA scanner crashed.**\n"
                f"Error: {type(exc).__name__}: {str(exc)[:700]}"
            )

            raise

        finally:
            if browser:
                await browser.close()


if __name__ == "__main__":
    asyncio.run(run_scan())
