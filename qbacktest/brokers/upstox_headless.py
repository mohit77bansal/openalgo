"""Headless Upstox OAuth via Playwright + TOTP.

Why this exists: Upstox v2's published auth flow is OAuth-redirect-only. The
official endpoints expect a human-in-the-browser to fill mobile + OTP/TOTP +
PIN. There is no documented headless API.

This module uses **the official Upstox login URLs** but drives them through a
headless Chromium controlled by Playwright, with TOTP computed locally from a
stored base32 secret. The redirect URL containing `?code=` is captured from
within the browser and handed back to the standard `UpstoxAdapter.exchange_code_for_token`.

Honest assessment of fragility:
- Selectors and form structure are NOT a stable contract. Upstox can update
  their login HTML and break this. Plan to maintain it.
- For production-grade autonomous trading, AngelOne SmartAPI's official TOTP
  endpoint (see `angelone.py`) is genuinely better.
- This path is for users who already have Upstox accounts funded and want
  zero-touch refresh once a day.

Pre-requisites:
1. Upstox account with **TOTP-based 2FA enabled** (NOT SMS OTP). Set up via
   Upstox app/web → Profile → Security.
2. Save the base32 TOTP secret immediately when shown — Upstox doesn't show it
   again without disabling+re-enabling 2FA.
3. `pip install playwright && playwright install chromium`. Both wrapped via
   `make install-broker`.

Required env vars:
    UPSTOX_CLIENT_ID, UPSTOX_CLIENT_SECRET, UPSTOX_REDIRECT_URI  (already used)
    UPSTOX_MOBILE       — 10-digit mobile number registered with Upstox
    UPSTOX_PIN          — 6-digit trading PIN
    UPSTOX_TOTP_SECRET  — base32 string from 2FA setup
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

from qbacktest.brokers.angelone import compute_totp  # reuse RFC 6238 implementation
from qbacktest.brokers.upstox import UpstoxAdapter
from qbacktest.core.exceptions import BrokerAuthError

log = logging.getLogger(__name__)


# Selector strategies ordered by preference. If Upstox changes their login HTML,
# update these. Keeping multiple per field gives us a graceful failure surface.
_MOBILE_SELECTORS = [
    'input[name="mobileNum"]',
    'input[type="tel"]',
    'input[placeholder*="Mobile" i]',
    'input[autocomplete="tel"]',
]
_GET_OTP_SELECTORS = [
    'button:has-text("Get OTP")',
    'button:has-text("Continue")',
    'button[type="submit"]',
]
_OTP_SELECTORS = [
    'input[name="otpNum"]',
    'input[autocomplete="one-time-code"]',
    'input[inputmode="numeric"][maxlength="6"]',
]
_PIN_SELECTORS = [
    'input[name="pin"]',
    'input[type="password"][maxlength="6"]',
    'input[placeholder*="PIN" i]',
]
_CONTINUE_SELECTORS = [
    'button:has-text("Continue")',
    'button:has-text("Login")',
    'button:has-text("Submit")',
    'button[type="submit"]',
]


@dataclass
class UpstoxHeadlessAuth:
    """Drives the Upstox OAuth login through Playwright."""

    adapter: UpstoxAdapter
    mobile: str
    pin: str
    totp_secret: str
    headless: bool = True
    timeout_ms: int = 30_000
    debug_screenshots_dir: Optional[str] = None
    _last_screenshots: list[str] = field(default_factory=list, init=False)

    async def login(self) -> dict:
        """Run the full headless flow. Returns the token-exchange response payload.

        Raises:
            BrokerAuthError if any step fails (with last screenshot path in message
            when `debug_screenshots_dir` is set).
        """
        # Validate credentials FIRST so we fail fast without paying for Playwright
        # import. Tests rely on this ordering.
        if not self.totp_secret:
            raise BrokerAuthError("UPSTOX_TOTP_SECRET not set — TOTP-based 2FA must be enabled on Upstox account")
        if not self.mobile:
            raise BrokerAuthError("UPSTOX_MOBILE not set")
        if not self.pin:
            raise BrokerAuthError("UPSTOX_PIN not set")

        try:
            from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout
        except ImportError as e:
            raise BrokerAuthError(
                "Playwright not installed. Run `pip install playwright && playwright install chromium` "
                "(or `make install-broker`)."
            ) from e

        auth_url = self.adapter.authorize_url()
        captured_code: list[str] = []

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.headless)
            context = await browser.new_context()
            page = await context.new_page()

            # Capture the auth code as soon as the browser tries to navigate to
            # our redirect URI (which our local server may or may not be running).
            redirect_host = urllib.parse.urlparse(self.adapter.redirect_uri).netloc

            async def on_request(request):
                u = request.url
                if redirect_host in u and "code=" in u:
                    qs = urllib.parse.urlparse(u).query
                    code = urllib.parse.parse_qs(qs).get("code", [None])[0]
                    if code:
                        captured_code.append(code)
                        # Abort the navigation — we don't need to actually hit the
                        # callback, we already have the code.
                        try:
                            await request.abort()
                        except Exception:
                            pass

            page.on("request", on_request)

            try:
                await self._run_flow(page, auth_url)
            except Exception as e:
                if self.debug_screenshots_dir:
                    path = await self._snap(page, "fatal")
                    raise BrokerAuthError(f"Headless login failed: {e}. See {path}") from e
                raise BrokerAuthError(f"Headless login failed: {e}") from e
            finally:
                await context.close()
                await browser.close()

        if not captured_code:
            raise BrokerAuthError(
                "Login appeared to succeed but no auth code was captured at the redirect URI. "
                "Verify UPSTOX_REDIRECT_URI matches what's registered on the Upstox developer console."
            )

        code = captured_code[0]
        log.info("Headless Upstox login captured auth code, exchanging for access token...")
        return await self.adapter.exchange_code_for_token(code)

    # --- Internal flow steps --------------------------------------------- #

    async def _run_flow(self, page, auth_url: str) -> None:
        from playwright.async_api import TimeoutError as PWTimeout

        await page.goto(auth_url, timeout=self.timeout_ms)
        await self._snap(page, "01-loaded")

        # Step 1: mobile number
        mobile_input = await self._find(page, _MOBILE_SELECTORS, what="mobile number input")
        await mobile_input.fill(self.mobile)
        await self._snap(page, "02-mobile-filled")

        get_otp = await self._find(page, _GET_OTP_SELECTORS, what="Get OTP / Continue button")
        await get_otp.click()
        await self._snap(page, "03-otp-requested")

        # Step 2: OTP — compute fresh TOTP, fill it in
        otp_input = await self._find(page, _OTP_SELECTORS, what="OTP input")
        await asyncio.sleep(0.5)  # let TOTP advance fully into a window
        totp = compute_totp(self.totp_secret)
        await otp_input.fill(totp)
        await self._snap(page, "04-otp-filled")

        # Some Upstox flows auto-submit on 6 digits, some require a button click.
        try:
            cont = await self._find(page, _CONTINUE_SELECTORS, what="Continue (post-OTP)", timeout_ms=3000)
            await cont.click()
        except (PWTimeout, BrokerAuthError):
            log.info("No explicit OTP-continue button; assuming auto-advance.")

        # Step 3: PIN
        pin_input = await self._find(page, _PIN_SELECTORS, what="PIN input", timeout_ms=15_000)
        await pin_input.fill(self.pin)
        await self._snap(page, "05-pin-filled")

        cont = await self._find(page, _CONTINUE_SELECTORS, what="Continue (post-PIN)")
        await cont.click()
        await self._snap(page, "06-pin-submitted")

        # Step 4: Approve consent screen if present.
        try:
            approve = await self._find(
                page, _CONTINUE_SELECTORS + ['button:has-text("Authorize")', 'button:has-text("Approve")'],
                what="Authorize / Continue (consent)", timeout_ms=8000,
            )
            await approve.click()
            await self._snap(page, "07-consent-approved")
        except (PWTimeout, BrokerAuthError):
            log.info("No consent screen — already authorized this app previously.")

        # Wait for the redirect navigation. We cap at 15s; on_request handler
        # will have captured the code by then if the flow succeeded.
        try:
            await page.wait_for_url(lambda u: "code=" in u, timeout=15_000)
        except PWTimeout:
            # Final flush — sometimes the redirect happens but page already
            # finished loading by the time we check.
            await self._snap(page, "08-after-wait")

    async def _find(self, page, selectors: list[str], *, what: str, timeout_ms: int = 15_000):
        """Try each selector in turn; return the first that resolves to a visible element."""
        last_err = None
        for sel in selectors:
            try:
                el = await page.wait_for_selector(sel, state="visible", timeout=timeout_ms // len(selectors))
                if el is not None:
                    return el
            except Exception as e:
                last_err = e
                continue
        raise BrokerAuthError(f"Could not find {what}. Tried selectors: {selectors}. Last error: {last_err}")

    async def _snap(self, page, name: str) -> Optional[str]:
        """Take a debug screenshot if a directory is configured."""
        if not self.debug_screenshots_dir:
            return None
        from pathlib import Path
        d = Path(self.debug_screenshots_dir)
        d.mkdir(parents=True, exist_ok=True)
        path = str(d / f"upstox-{name}.png")
        try:
            await page.screenshot(path=path, full_page=True)
            self._last_screenshots.append(path)
            log.debug("debug screenshot: %s", path)
        except Exception:
            pass
        return path
