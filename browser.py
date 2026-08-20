"""
Playwright HTTP client (real Chromium)
--------------------------------------
Used for sites where TLS impersonation is not enough because the CDN
runs a JavaScript bot challenge (Akamai on Zoro). curl-impersonate
cannot execute that JS, so product pages 403. A real browser can.

Same interface as HttpClient / CurlClient:
    client.get(url) -> str | None
    client.get_bytes(url) -> bytes | None
    client.set_cookie / set_header / close
"""

from __future__ import annotations

import logging
import os
import random
import time
from urllib.parse import urlparse

log = logging.getLogger("scraper.browser")

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = { runtime: {} };
"""


class PlaywrightClient:
    def __init__(
        self,
        delay: float = 2.5,
        timeout: int = 45,
        respect_robots: bool = True,
        headless: bool = True,
        proxy: str | None = None,
        use_env_proxy: bool = False,
    ) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise RuntimeError(
                "playwright is not installed. On the VPS run:\n"
                "  venv/bin/pip install playwright\n"
                "  sudo venv/bin/playwright install-deps chromium\n"
                "  venv/bin/playwright install chromium"
            ) from e

        self.delay = delay
        self.timeout = timeout * 1000  # ms
        self.respect_robots = respect_robots
        self._extra_headers: dict[str, str] = {}
        self._last_request = 0.0
        self._warmed_hosts: set[str] = set()

        # Default OFF for Zoro: WARP (Cloudflare IPs) is also 403'd by
        # Akamai. Direct datacenter + real Chrome JS challenge is the
        # free path. Pass use_env_proxy=True only if you have a
        # residential proxy in HTTP_PROXY_URL.
        raw_proxy = proxy
        if use_env_proxy and not raw_proxy:
            raw_proxy = (
                os.environ.get("HTTP_PROXY_URL")
                or os.environ.get("HTTPS_PROXY")
                or os.environ.get("HTTP_PROXY")
                or None
            )

        self._pw = sync_playwright().start()
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ]
        launch_kwargs: dict = {
            "headless": headless,
            "args": launch_args,
        }
        if raw_proxy:
            server = raw_proxy.replace("socks5h://", "socks5://")
            launch_kwargs["proxy"] = {"server": server}
            log.info("Playwright proxy in use (host redacted)")
        else:
            log.info("Playwright launching Chromium direct (no proxy)")

        try:
            self._browser = self._pw.chromium.launch(**launch_kwargs)
        except Exception as e:  # noqa: BLE001
            self._pw.stop()
            raise RuntimeError(
                f"Chromium failed to launch ({e}). Install it with:\n"
                "  sudo venv/bin/playwright install-deps chromium\n"
                "  venv/bin/playwright install chromium"
            ) from e

        self._context = self._browser.new_context(
            user_agent=DEFAULT_UA,
            viewport={"width": 1366, "height": 768},
            locale="en-US",
            timezone_id="America/Chicago",
        )
        self._context.add_init_script(_STEALTH_JS)
        self._page = self._context.new_page()
        log.info("Playwright Chromium ready")

    def set_cookie(self, name: str, value: str, domain: str | None = None) -> None:
        cookie: dict = {"name": name, "value": value, "path": "/"}
        if domain:
            cookie["domain"] = domain
        else:
            cookie["url"] = "https://www.zoro.com/"
        try:
            self._context.add_cookies([cookie])
        except Exception as e:  # noqa: BLE001
            log.warning("Could not set cookie %s: %s", name, e)

    def set_header(self, name: str, value: str) -> None:
        self._extra_headers[name] = value

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request
        wait = self.delay - elapsed
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.4))
        self._last_request = time.time()

    def _warmup(self, url: str) -> None:
        """Hit the origin homepage once so Akamai can drop a clearance cookie."""
        host = urlparse(url).netloc.lower()
        if not host or host in self._warmed_hosts:
            return
        origin = f"{urlparse(url).scheme or 'https'}://{urlparse(url).netloc}/"
        log.info("Playwright warmup %s (Akamai JS challenge)", origin)
        try:
            self._page.goto(origin, wait_until="domcontentloaded", timeout=self.timeout)
            self._page.wait_for_timeout(4000)
            title = (self._page.title() or "").lower()
            html = self._page.content()[:800].lower()
            if "access denied" in html or "denied" in title:
                log.warning("Warmup still looks blocked (title=%r) — continuing anyway",
                            self._page.title())
            else:
                log.info("Warmup OK (title=%r)", self._page.title()[:80])
        except Exception as e:  # noqa: BLE001
            log.warning("Warmup failed for %s: %s", origin, e)
        self._warmed_hosts.add(host)

    def _blocked(self, html: str, status: int | None) -> bool:
        if status in (403, 429, 503):
            return True
        head = (html or "")[:2000].lower()
        if "access denied" in head and "akamai" in head:
            return True
        if "<title>zoro.com</title>" in head and "product" not in head and len(html) < 5000:
            # Short generic title + tiny body is the Akamai brick wall we saw
            # (771 bytes, title "zoro.com") — not a real product page.
            return True
        return False

    def get(self, url: str) -> str | None:
        data = self.get_bytes(url)
        if data is None:
            return None
        return data.decode("utf-8", errors="ignore")

    def get_bytes(self, url: str) -> bytes | None:
        self._throttle()
        self._warmup(url)
        try:
            if self._extra_headers:
                self._page.set_extra_http_headers(self._extra_headers)
            resp = self._page.goto(
                url, wait_until="domcontentloaded", timeout=self.timeout,
            )
            status = resp.status if resp is not None else None
            # Give Akamai sensor JS a moment on first product hits.
            self._page.wait_for_timeout(2500)
            html = self._page.content()
            if self._blocked(html, status):
                log.info("Playwright got HTTP %s — waiting for challenge on %s",
                         status, url)
                self._page.wait_for_timeout(6000)
                html = self._page.content()
                status2 = None
                try:
                    status2 = self._page.evaluate("() => document.readyState")
                except Exception:
                    pass
                if self._blocked(html, status):
                    log.error("HTTP %s for %s (playwright, still blocked, ready=%s)",
                              status, url, status2)
                    return None
            if len(html) < 200:
                log.warning("Empty Playwright body for %s", url)
                return None
            return html.encode("utf-8", errors="ignore")
        except Exception as e:  # noqa: BLE001
            log.warning("Playwright fetch error for %s: %s", url, e)
            return None

    def close(self) -> None:
        try:
            self._context.close()
        except Exception:
            pass
        try:
            self._browser.close()
        except Exception:
            pass
        try:
            self._pw.stop()
        except Exception:
            pass
        log.info("Playwright closed")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
