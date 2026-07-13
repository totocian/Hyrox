#!/usr/bin/env python3
"""
HYROX Perth - Men's Doubles Open ticket watcher.

Checks the non-charity "HYROX DOUBLES MEN | Friday/Saturday/Sunday" tickets
(Doubles > Open > Men) on the AirAsia HYROX Perth vivenu ticket widget, and
sends an email the moment any of the three dates is no longer sold out.

Designed to be run on a schedule (e.g. a GitHub Actions workflow) so it
works with no computer or Claude open.

Requires: playwright (with chromium installed), python-dotenv
    pip install -r requirements.txt
    playwright install --with-deps chromium

Configuration is via environment variables / a .env file next to this
script - see .env.example.
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
import smtplib

from dotenv import load_dotenv
from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

EVENT_URL = "https://australia.hyrox.com/event/hyrox-perth-season-26-27-puy2kq?useEmbed=true"
BUY_URL = "https://australia.hyrox.com/event/hyrox-perth-season-26-27-puy2kq"

# Exact non-charity ticket titles we care about (order = Fri, Sat, Sun).
TARGETS = {
    "HYROX DOUBLES MEN | Friday": "Fri 21 Aug 2026",
    "HYROX DOUBLES MEN | Saturday": "Sat 22 Aug 2026",
    "HYROX DOUBLES MEN | Sunday": "Sun 23 Aug 2026",
}

STATE_FILE = BASE_DIR / "state.json"
LOG_FILE = BASE_DIR / "monitor.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("hyrox-watch")


async def _first_visible(ctx, text: str, exact: bool = False, timeout: int = 20000):
    """Poll for a VISIBLE element matching this text, checking each
    candidate's actual visibility via Playwright's own is_visible().

    vivenu's widget appears to render duplicate hidden copies of some markup
    (e.g. for responsive/mobile layouts), so plain get_by_text(...).first can
    lock onto a hidden duplicate and wait forever even though a visible
    match exists elsewhere in the DOM. Rather than trust selector-string
    tricks to filter by visibility (which turned out not to work as
    expected), this checks each match directly in Python."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout / 1000
    loc = ctx.get_by_text(text, exact=exact)
    last_err = None
    while True:
        try:
            count = await loc.count()
            for i in range(count):
                candidate = loc.nth(i)
                try:
                    if await candidate.is_visible():
                        return candidate
                except Exception as e:
                    last_err = e
        except Exception as e:
            last_err = e
        if loop.time() > deadline:
            raise TimeoutError(
                f"No visible element found for text={text!r} within {timeout}ms"
                + (f" (last error: {last_err})" if last_err else "")
            )
        await asyncio.sleep(0.3)


async def _click_visible(ctx, text: str, exact: bool = False, timeout: int = 20000):
    el = await _first_visible(ctx, text, exact=exact, timeout=timeout)
    await el.click(timeout=5000)


async def _wait_visible(ctx, text: str, exact: bool = False, timeout: int = 20000):
    await _first_visible(ctx, text, exact=exact, timeout=timeout)


async def _find_frame_with_text(page, text: str, timeout: int = 20000):
    """Poll every frame of the page (main document plus any iframe- or
    <object>-embedded document) for one that actually contains this text,
    and return it.

    This ticket widget is embedded via an <object data="https://.../checkout/...">
    tag - NOT an iframe, and the embedded document lives on the SAME
    australia.hyrox.com domain, not vivenu.com. A fixed URL-substring check
    (e.g. "vivenu" in frame.url) never matches it, which silently left every
    prior version of this script operating on the wrong document the whole
    time. Checking actual rendered content per-frame is robust regardless of
    how the widget happens to be embedded."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout / 1000
    last_err = None
    while True:
        for f in page.frames:
            try:
                count = await f.get_by_text(text, exact=False).count()
                if count > 0:
                    return f
            except Exception as e:
                last_err = e
        if loop.time() > deadline:
            raise TimeoutError(
                f"No frame found containing text={text!r} within {timeout}ms"
                + (f" (last error: {last_err})" if last_err else "")
            )
        await asyncio.sleep(0.3)


async def fetch_ticket_text() -> str:
    """Open the ticket widget, drill into Doubles > Open > Men, and return
    the plain text of the ticket list so we can look for SOLD OUT badges."""
    async with async_playwright() as p:
        # NOTE: deliberately NOT using --single-process - it's known to break
        # iframe/embedded-widget rendering in headless Chromium, which is
        # exactly what this ticket widget is. --no-sandbox and
        # --disable-dev-shm-usage are the standard, safe flags for running
        # headless Chromium on small/low-memory Linux boxes.
        # --disable-blink-features=AutomationControlled + the navigator.webdriver
        # override below reduce the odds of the ticketing widget's bot/WAF
        # protection treating a well-known cloud-runner IP (e.g. GitHub
        # Actions) with extra suspicion versus a residential/VPS IP.
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        page = await browser.new_page(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            locale="en-AU",
            timezone_id="Australia/Perth",
        )
        await page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        try:
            await page.goto(EVENT_URL, wait_until="domcontentloaded", timeout=45000)

            await page.get_by_role("button", name="Buy tickets").first.click(timeout=20000)

            # Find the actual frame the ticket-selection widget rendered
            # into (see _find_frame_with_text docstring for why this can't
            # just be page.frames filtered by a URL substring).
            target = await _find_frame_with_text(page, "Select a category", timeout=20000)

            await _click_visible(target, "Doubles", exact=True)
            await _wait_visible(target, "Class")

            await _click_visible(target, "Open", exact=True)
            await _wait_visible(target, "Gender")

            await _click_visible(target, "Men", exact=True)
            await _wait_visible(target, "HYROX DOUBLES MEN | Friday")

            text = await target.locator("body").inner_text()
            return text
        except Exception:
            try:
                shot_path = BASE_DIR / "debug_last_failure.png"
                await page.screenshot(path=str(shot_path), full_page=True)
                log.error("Saved a debug screenshot to %s (uploaded as a workflow artifact on failure).", shot_path)
            except Exception:
                log.exception("Also failed to capture a debug screenshot")
            raise
        finally:
            await browser.close()


def parse_availability(page_text: str) -> dict:
    """Return {ticket title: True/False available} for each target ticket.
    A ticket is considered SOLD OUT if that literal badge text appears in the
    two lines immediately preceding its title in the rendered widget."""
    lines = [l.strip() for l in page_text.splitlines() if l.strip()]
    results = {}
    for i, line in enumerate(lines):
        if line in TARGETS:
            preceding = lines[max(0, i - 2): i]
            sold_out = any("SOLD OUT" in p.upper() for p in preceding)
            results[line] = not sold_out
    return results


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def send_email(subject: str, body: str) -> None:
    addr = os.environ.get("GMAIL_ADDRESS")
    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    to_addr = os.environ.get("NOTIFY_EMAIL_TO", addr)
    if not addr or not app_password:
        log.warning("Email not configured (GMAIL_ADDRESS/GMAIL_APP_PASSWORD missing) - skipping email.")
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = addr
    msg["To"] = to_addr
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.starttls()
            server.login(addr, app_password)
            server.sendmail(addr, [to_addr], msg.as_string())
        log.info("Email sent to %s", to_addr)
    except Exception:
        log.exception("Failed to send email")


async def main() -> int:
    """Returns 0 on a successful check (regardless of ticket availability),
    non-zero if the check itself failed - so CI actually reports failure
    instead of a false-green run."""
    log.info("Checking HYROX Perth Men's Doubles Open availability...")
    try:
        page_text = await asyncio.wait_for(fetch_ticket_text(), timeout=90)
    except asyncio.TimeoutError:
        log.error("Timed out after 90s loading/clicking through the ticket widget - aborting this run.")
        return 1
    except Exception:
        log.exception("Failed to load/parse ticket widget this run")
        return 1

    availability = parse_availability(page_text)

    missing = [t for t in TARGETS if t not in availability]
    if missing:
        log.warning("Could not locate these ticket rows on the page (site layout may have changed): %s", missing)

    if not availability:
        log.error("No target ticket rows found at all - the page structure likely changed. No notification sent.")
        return 1

    available_now = {t: ok for t, ok in availability.items() if ok}
    state = load_state()

    log.info("Status: %s", {t: ("AVAILABLE" if ok else "sold out") for t, ok in availability.items()})

    if available_now:
        lines = [f"- {TARGETS[t]} ({t.split('| ')[-1]}) is AVAILABLE" for t in available_now]
        body = (
            "HYROX Perth - Men's Doubles Open tickets just opened up!\n\n"
            + "\n".join(lines)
            + f"\n\nBuy now before it sells out again: {BUY_URL}\n"
            + f"\nChecked at {datetime.now(timezone.utc).isoformat()}"
        )
        send_email("HYROX Perth Doubles Men tickets AVAILABLE!", body)
    else:
        log.info("Still sold out for all three dates - no notification sent.")

    state["available"] = list(available_now.keys())
    state["last_checked"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
