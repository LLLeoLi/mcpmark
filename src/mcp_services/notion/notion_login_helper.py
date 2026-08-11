"""
Notion Login Helper for MCPMark
=================================

This module provides a utility class and CLI script for logging into Notion
using Playwright. It saves the authenticated session state to a file,
which can be used for subsequent automated tasks.
"""

import argparse
from pathlib import Path
from typing import Optional

from playwright.sync_api import (
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from src.base.login_helper import BaseLoginHelper
from src.logger import get_logger

# Initialize logger
logger = get_logger(__name__)

# Notion's session cookie. Without it the saved storage state is anonymous and
# every downstream Playwright automation lands on the login page instead of the
# actual Notion page.
SESSION_COOKIE_NAME = "token_v2"

# Authenticated-only endpoints used to confirm the session really works.
# Return 200 with the user's spaces when logged in, 401 otherwise. app.notion.com
# is tried first: it is the domain the session cookies now live on, and the
# legacy notion.so domain is unreachable from some networks.
# (www.notion.com is the marketing site and 404s here — do not add it.)
GET_SPACES_ENDPOINTS = (
    "https://app.notion.com/api/v3/getSpaces",
    "https://www.notion.so/api/v3/getSpaces",
)


class NotionLoginError(RuntimeError):
    """Raised when the browser session is not authenticated with Notion."""


def _extract_space_names(payload: object) -> list[str]:
    """Pulls workspace names out of a getSpaces response, tolerating shape drift."""
    names: list[str] = []
    if not isinstance(payload, dict):
        return names
    for user_blob in payload.values():
        if not isinstance(user_blob, dict):
            continue
        for space in (user_blob.get("space") or {}).values():
            value = (space or {}).get("value") or {}
            if "name" not in value and isinstance(value.get("value"), dict):
                value = value["value"]
            name = value.get("name")
            if name:
                names.append(name)
    return names


class NotionLoginHelper(BaseLoginHelper):
    """
    Utility helper for logging into Notion using Playwright.
    """

    SUPPORTED_BROWSERS = {"chromium", "firefox"}

    def __init__(
        self,
        *,
        url: Optional[str] = None,
        headless: bool = True,
        state_path: Optional[str | Path] = None,
        browser: str = "firefox",
    ) -> None:
        """
        Initializes the Notion login helper.

        Args:
            url: The Notion URL to open after launching the browser.
            headless: Whether to run Playwright in headless mode.
            state_path: The path to save the authenticated session state.
            browser: The browser engine to use ('chromium' or 'firefox').
        """
        super().__init__()
        if browser not in self.SUPPORTED_BROWSERS:
            raise ValueError(
                f"Unsupported browser '{browser}'. Supported browsers are: {', '.join(self.SUPPORTED_BROWSERS)}"
            )

        self.url = url or "https://www.notion.so/login"
        self.headless = headless
        self.browser_name = browser
        self.state_path = (
            Path(state_path or Path.cwd() / "notion_state.json").expanduser().resolve()
        )
        self._browser_context: Optional[BrowserContext] = None
        self._playwright = None
        self._browser = None

    def login(self) -> BrowserContext:
        """
        Launches a browser, performs login, and saves the session state.

        The state file is only written after the session is confirmed to be
        authenticated, so a failed login never clobbers a working state file.
        """
        if self._playwright is None:
            self._playwright = sync_playwright().start()

        browser_type = getattr(self._playwright, self.browser_name)
        self._browser = browser_type.launch(headless=self.headless)
        context = self._browser.new_context()
        page = context.new_page()

        logger.info("Navigating to Notion URL: %s", self.url)
        page.goto(self.url, wait_until="load")

        if self.headless:
            self._handle_headless_login(context)
        else:
            logger.info(
                "A browser window has been opened. Please complete the Notion login."
            )
            logger.info(
                "After you see your workspace, return to this terminal and press <ENTER>."
            )
            initial_url = page.url
            input()
            try:
                page.wait_for_url(lambda u: u != initial_url, timeout=10_000)
            except PlaywrightTimeoutError:
                pass  # It's okay if the URL doesn't change

        try:
            page.wait_for_load_state("domcontentloaded", timeout=5_000)
        except PlaywrightTimeoutError:
            pass

        # Fail loudly instead of persisting an anonymous session.
        spaces = self._verify_session(context)

        context.storage_state(path=str(self.state_path))
        logger.info(
            "✅ Login verified (workspace: %s). Session state saved to %s",
            ", ".join(spaces) if spaces else "unknown",
            self.state_path,
        )

        self._browser_context = context
        return context

    def _verify_session(self, context: BrowserContext) -> list[str]:
        """
        Confirms the context holds a real Notion session.

        Returns the workspace names reachable by the session.

        Raises:
            NotionLoginError: if the session is anonymous or the check fails.
        """
        cookie_names = {c.get("name") for c in context.cookies()}
        if SESSION_COOKIE_NAME not in cookie_names:
            raise NotionLoginError(
                f"Login did not complete: session cookie '{SESSION_COOKIE_NAME}' is "
                "missing, so the saved state would be anonymous. If this account "
                "signs in with Google/Apple/SSO or 2FA, the headless email-code flow "
                "cannot work — run the helper without --headless on a machine with a "
                "display and copy notion_state.json over."
            )

        response = None
        unreachable = []
        for endpoint in GET_SPACES_ENDPOINTS:
            try:
                candidate = context.request.post(endpoint, data={}, timeout=20_000)
            except Exception as e:
                # Network-level failure (blocked domain, proxy, timeout). Try the
                # next endpoint rather than blaming the session.
                unreachable.append(f"{endpoint} ({type(e).__name__})")
                continue
            if candidate.status != 200:
                raise NotionLoginError(
                    f"Session rejected by Notion: {endpoint} returned HTTP "
                    f"{candidate.status}. The '{SESSION_COOKIE_NAME}' cookie is "
                    "present but not valid (expired or logged out elsewhere)."
                )
            response = candidate
            break

        if response is None:
            # The cookie check already passed; a dead network is not proof of a
            # dead session, so warn instead of discarding a possibly-good login.
            logger.warning(
                "Could not reach Notion to verify the session online (%s). The "
                "'%s' cookie is present, so the state is probably usable — re-run "
                "with --check from a machine with working network access.",
                "; ".join(unreachable),
                SESSION_COOKIE_NAME,
            )
            return []

        try:
            spaces = _extract_space_names(response.json())
        except Exception as e:
            logger.warning("Session verified, but could not parse workspaces: %s", e)
            return []

        if not spaces:
            logger.warning(
                "Session is authenticated but reports no workspaces — check that the "
                "account you logged in with actually owns the MCPMark hubs."
            )
        return spaces

    def verify_state_file(self) -> list[str]:
        """
        Checks an existing state file without performing a login.

        Returns the workspace names reachable by the stored session.

        Raises:
            NotionLoginError: if the file is missing or the session is dead.
        """
        if not self.state_path.exists():
            raise NotionLoginError(f"State file not found: {self.state_path}")

        if self._playwright is None:
            self._playwright = sync_playwright().start()

        browser_type = getattr(self._playwright, self.browser_name)
        self._browser = browser_type.launch(headless=True)
        context = self._browser.new_context(storage_state=str(self.state_path))
        try:
            return self._verify_session(context)
        finally:
            context.close()

    def close(self) -> None:
        """Closes the underlying browser and Playwright instance."""
        if self._browser_context:
            try:
                self._browser_context.close()
            finally:
                self._browser_context = None
        if self._browser:
            try:
                self._browser.close()
            finally:
                self._browser = None
        if self._playwright:
            self._playwright.stop()
            self._playwright = None

    def _handle_headless_login(self, context: BrowserContext) -> None:
        """
        Guides the user through the login process in headless mode.
        """
        page: Page = context.pages[0]
        login_url = "https://www.notion.so/login"
        page.goto(login_url, wait_until="domcontentloaded")

        email = input("Enter your Notion email address: ").strip()
        try:
            email_input = page.locator(
                'input[placeholder="Enter your email address..."]'
            )
            email_input.wait_for(state="visible", timeout=120_000)
            email_input.fill(email)
            email_input.press("Enter")
        except PlaywrightTimeoutError:
            raise RuntimeError("Timed out waiting for the email input field.")
        except Exception:
            page.get_by_role("button", name="Continue", exact=True).click()

        try:
            code_input = page.locator('input[placeholder="Enter code"]')
            code_input.wait_for(state="visible", timeout=120_000)
            code = input("Enter the verification code from your email: ").strip()
            code_input.fill(code)
            code_input.press("Enter")
        except PlaywrightTimeoutError:
            raise RuntimeError("Timed out waiting for the verification code input.")
        except Exception:
            page.get_by_role("button", name="Continue", exact=True).click()

        try:
            page.wait_for_url(lambda url: url != login_url, timeout=180_000)
        except PlaywrightTimeoutError:
            logger.warning("Login redirect timed out, but proceeding to save state.")

        if self.url and self.url != login_url:
            page.goto(self.url, wait_until="domcontentloaded")

    def __enter__(self) -> "NotionLoginHelper":
        self.login()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


def main():
    """Main entry point for the Notion login CLI script."""
    parser = argparse.ArgumentParser(
        description="Authenticate to Notion and generate a session state file.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run the login flow in headless mode (prompts for credentials).",
    )
    parser.add_argument(
        "--browser",
        default="firefox",
        choices=["chromium", "firefox"],
        help="The browser engine to use for Playwright.",
    )
    parser.add_argument(
        "--state-path",
        default=None,
        help="Path to the session state file (default: ./notion_state.json).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Only verify the existing state file; do not log in or overwrite it.",
    )
    args = parser.parse_args()

    helper = NotionLoginHelper(
        headless=args.headless, browser=args.browser, state_path=args.state_path
    )

    if args.check:
        try:
            spaces = helper.verify_state_file()
        except NotionLoginError as e:
            logger.error("❌ %s", e)
            raise SystemExit(1)
        finally:
            helper.close()
        logger.info(
            "✅ %s is authenticated (workspace: %s).",
            helper.state_path,
            ", ".join(spaces) if spaces else "unknown",
        )
        return

    try:
        with helper:
            logger.info("Login process completed.")
    except NotionLoginError as e:
        logger.error("❌ %s", e)
        logger.error("State file was NOT written: %s", helper.state_path)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
