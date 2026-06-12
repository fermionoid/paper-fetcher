"""EZproxy authentication management using Selenium."""

import json
import logging
import time
from pathlib import Path

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

from .config import Config

logger = logging.getLogger(__name__)

# URL used to test if EZproxy session is valid
TEST_URL = "https://www.nature.com"
EZPROXY_DOMAIN = "eproxy.lib.hku.hk"


class EZProxyAuth:
    """Manages EZproxy authentication via Selenium and cookie persistence."""

    def __init__(self, config: Config | None = None):
        self.config = config or Config()
        self.config.ensure_dirs()
        self._session: requests.Session | None = None
        self._driver: webdriver.Chrome | None = None
        # Set True when a login was needed but skipped/failed in non-interactive
        # mode (e.g. MCP server). Lets callers surface a "please log in" message
        # instead of silently returning empty results.
        self.session_expired = False

    @property
    def session(self) -> requests.Session:
        """Get an authenticated requests session."""
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            })
        return self._session

    def login(self, force: bool = False, interactive: bool = True) -> bool:
        """Ensure we have a valid EZproxy session.

        If cookies exist and are valid, reuses them.
        Otherwise opens a browser for manual login (interactive mode only).

        Args:
            force: If True, ignore saved cookies and force re-login.
            interactive: If False, never open a browser. When cookies are
                missing/expired, set session_expired and return False so the
                caller can ask the user to run `paper-fetcher login` instead
                of blocking on a browser (used by the MCP server).

        Returns:
            True if authentication succeeded.
        """
        if not force and self._try_load_cookies():
            logger.info("Loaded saved cookies - session is valid.")
            self.session_expired = False
            return True

        if not interactive:
            logger.warning(
                "EZproxy session missing/expired and running non-interactively "
                "- skipping browser login."
            )
            self.session_expired = True
            return False

        logger.info("No valid session found. Opening browser for login...")
        ok = self._browser_login()
        self.session_expired = not ok
        return ok

    def _try_load_cookies(self) -> bool:
        """Try to load cookies from file and validate them."""
        cookie_path = Path(self.config.cookie_path)
        if not cookie_path.exists():
            return False

        try:
            cookies = json.loads(cookie_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to read cookies: %s", e)
            return False

        # Load cookies into session
        for cookie in cookies:
            self.session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain", ""),
                path=cookie.get("path", "/"),
            )

        # Validate by making a test request
        return self._validate_session()

    def _validate_session(self) -> bool:
        """Check if the current session can access proxied content."""
        proxy_url = self.config.proxy_base + TEST_URL
        try:
            resp = self.session.get(proxy_url, timeout=15, allow_redirects=True)
            # If we end up on the login page, session is invalid
            if "login" in resp.url.lower() and EZPROXY_DOMAIN in resp.url:
                logger.info("Session expired - cookies are no longer valid.")
                return False
            # Check if we got proxied content (domain rewritten)
            if EZPROXY_DOMAIN in resp.url or resp.status_code == 200:
                return True
        except requests.RequestException as e:
            logger.warning("Session validation failed: %s", e)
        return False

    def _browser_login(self) -> bool:
        """Open Chrome for manual EZproxy login."""
        options = Options()
        # Don't use --user-data-dir to avoid conflicts with running Chrome
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--remote-allow-origins=*")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])

        try:
            service = Service(ChromeDriverManager().install())
            self._driver = webdriver.Chrome(service=service, options=options)
        except Exception as e:
            logger.error("Failed to start Chrome: %s", e)
            logger.error(
                "Make sure Chrome is installed and no other ChromeDriver "
                "instances are running. ChromeDriver is downloaded automatically."
            )
            return False

        # Navigate to EZproxy login
        login_url = self.config.proxy_base + TEST_URL
        self._driver.get(login_url)

        print("\n" + "=" * 60)
        print("  Please log in to HKU EZproxy in the browser window.")
        print("  The tool will detect when login is complete.")
        print("=" * 60 + "\n")

        # Poll until login succeeds
        max_wait = 600  # 10 minutes
        poll_interval = 3
        elapsed = 0
        last_url = ""

        while elapsed < max_wait:
            time.sleep(poll_interval)
            elapsed += poll_interval

            try:
                current_url = self._driver.current_url

                # Log URL changes so user can debug
                if current_url != last_url:
                    logger.info("Browser URL: %s", current_url)
                    last_url = current_url

                # Detection: URL contains eproxy domain and is not a login page
                if EZPROXY_DOMAIN in current_url and "login" not in current_url.lower():
                    logger.info("Login detected! URL: %s", current_url)
                    self._save_browser_cookies()
                    print("\n  Login successful! Cookies saved.\n")
                    self._close_browser()
                    return True

                # Detection: URL was rewritten (e.g. www-nature-com.eproxy...)
                if ".eproxy.lib.hku.hk" in current_url:
                    logger.info("Login detected via rewritten URL: %s", current_url)
                    self._save_browser_cookies()
                    print("\n  Login successful! Cookies saved.\n")
                    self._close_browser()
                    return True

                # Detection: check cookies for EZproxy session cookie
                cookies = self._driver.get_cookies()
                ez_cookies = [c for c in cookies if "ezproxy" in c.get("domain", "").lower()]
                if ez_cookies:
                    logger.info("Login detected via EZproxy cookies.")
                    self._save_browser_cookies()
                    print("\n  Login successful! Cookies saved.\n")
                    self._close_browser()
                    return True

            except Exception:
                # Browser might have been closed by user
                logger.warning("Browser connection lost.")
                self._driver = None
                return False

        print("\n  Login timed out after 10 minutes.\n")
        self._close_browser()
        return False

    def _save_browser_cookies(self):
        """Save cookies from Selenium browser to file and load into requests session."""
        if not self._driver:
            return

        cookies = self._driver.get_cookies()
        cookie_path = Path(self.config.cookie_path)
        cookie_path.write_text(
            json.dumps(cookies, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        logger.info("Saved %d cookies to %s", len(cookies), cookie_path)

        # Also load into requests session
        for cookie in cookies:
            self.session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain", ""),
                path=cookie.get("path", "/"),
            )

    def _close_browser(self):
        """Close the Selenium browser."""
        if self._driver:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None

    def get_proxied_url(self, url: str) -> str:
        """Convert a regular URL to an EZproxy URL."""
        if EZPROXY_DOMAIN in url:
            return url  # Already proxied
        return self.config.proxy_base + url

    def fetch(self, url: str, **kwargs) -> requests.Response:
        """Fetch a URL through the authenticated session.

        Automatically converts to EZproxy URL if needed.
        """
        proxied = self.get_proxied_url(url)
        kwargs.setdefault("timeout", 30)
        kwargs.setdefault("allow_redirects", True)
        return self.session.get(proxied, **kwargs)

    def close(self):
        """Clean up resources."""
        self._close_browser()
        if self._session:
            self._session.close()
            self._session = None
