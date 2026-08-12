"""
Curl-based HTTP Client
----------------------
Uses the system's `curl` binary (curl.exe on Windows 10+, curl on Linux/Mac)
as the actual fetcher.

Why this works when Python requests doesn't:
  * curl uses the OS's native TLS stack (Schannel on Windows, OpenSSL elsewhere).
    Its TLS fingerprint matches millions of legitimate users and CLI tools —
    Cloudflare's basic bot detection doesn't flag it aggressively.
  * requests uses Python-specific TLS defaults with a distinctive JA3
    fingerprint that anti-bot systems have learned to identify.

Best for:
  * iHerb via pk.iherb.com (regional subdomain has weaker anti-bot rules)
  * Any site with only basic Cloudflare Bot Fight Mode

Not a fix for:
  * Sites with advanced fingerprinting (Datadome, PerimeterX, Kasada)
  * Sites requiring JavaScript execution to render content
  * Sites that check for browser-specific headers we don't send

Same interface as HttpClient / PlaywrightClient:
    client.get(url)          -> str | None
    client.get_bytes(url)    -> bytes | None
    client.set_cookie(name, value, domain=...)
    client.set_header(name, value)
    client.close()
"""

from __future__ import annotations

import logging
import random
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

log = logging.getLogger("scraper.curl")

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)


class CurlClient:
    """
    HTTP client that shells out to curl for each request. Retains the same
    interface as HttpClient so it drops in cleanly.

    Session state (cookies) is persisted via curl's cookie jar file so that
    Cloudflare's clearance cookies stick across requests.
    """

    def __init__(
        self,
        delay: float = 2.0,
        timeout: int = 30,
        respect_robots: bool = True,
    ) -> None:
        # Verify curl is available
        curl_bin = shutil.which("curl") or shutil.which("curl.exe")
        if not curl_bin:
            raise RuntimeError(
                "curl not found on PATH. Windows 10 build 1803+ ships with "
                "curl.exe. On older systems: https://curl.se/windows/. "
                "Linux: apt install curl. Mac: curl is preinstalled."
            )
        self._curl = curl_bin

        try:
            v = subprocess.run(
                [self._curl, "--version"],
                capture_output=True, timeout=5, check=True, text=True,
            )
            log.info("curl ready: %s", v.stdout.splitlines()[0])
        except (subprocess.SubprocessError, FileNotFoundError) as e:
            raise RuntimeError(f"curl exists but failed to run: {e}") from e

        self.delay = delay
        self.timeout = timeout
        self.respect_robots = respect_robots
        self._robots_cache: dict[str, RobotFileParser] = {}
        self._last_request = 0.0
        self._extra_headers: dict[str, str] = {}
        self._extra_cookies: dict[str, str] = {}

        # Persistent cookie jar for Cloudflare clearance etc.
        self._tmp_dir = Path(tempfile.mkdtemp(prefix="curl_client_"))
        self._cookie_jar = self._tmp_dir / "cookies.txt"

    # ---- HttpClient-compatible interface ---------------------------- #

    def set_cookie(self, name: str, value: str, domain: str | None = None) -> None:
        """Cookie will be sent with every subsequent request."""
        self._extra_cookies[name] = value

    def set_header(self, name: str, value: str) -> None:
        self._extra_headers[name] = value

    def get(self, url: str) -> str | None:
        data = self._fetch(url)
        if data is None:
            return None
        return data.decode("utf-8", errors="ignore")

    def get_bytes(self, url: str) -> bytes | None:
        return self._fetch(url)

    def close(self) -> None:
        """Clean up cookie jar and temp files."""
        try:
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
        except Exception:
            pass

    # ---- internals -------------------------------------------------- #

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request
        wait = self.delay - elapsed
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.5))
        self._last_request = time.time()

    def _robots_ok(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parsed = urlparse(url)
        path = parsed.path.lower()
        if path.endswith("/robots.txt") or "sitemap" in path:
            return True
        base = f"{parsed.scheme}://{parsed.netloc}"
        rp = self._robots_cache.get(base)
        if rp is None:
            rp = RobotFileParser()
            rp.set_url(f"{base}/robots.txt")
            try:
                rp.read()
            except Exception:
                self._robots_cache[base] = rp
                return True
            self._robots_cache[base] = rp
        return rp.can_fetch(DEFAULT_UA, url)

    def _build_command(self, url: str, output_path: Path) -> list[str]:
        parsed = urlparse(url)
        default_referer = f"{parsed.scheme}://{parsed.netloc}/"
        referer = self._extra_headers.get("Referer", default_referer)

        cmd = [
            self._curl,
            "-L",                    # follow redirects
            "--compressed",          # accept gzip/br transport encoding
            "--silent",
            "--show-error",
            "-o", str(output_path),
            "-A", DEFAULT_UA,
            "-H", ("Accept: text/html,application/xhtml+xml,"
                   "application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"),
            "-H", "Accept-Language: en-US,en;q=0.9",
            "-H", f"Referer: {referer}",
            "-H", "Cache-Control: no-cache",
            "-b", str(self._cookie_jar),   # read cookies
            "-c", str(self._cookie_jar),   # write cookies back
            "--max-time", str(self.timeout),
        ]

        # Inline cookies from set_cookie() — combines with jar
        if self._extra_cookies:
            cookie_str = "; ".join(f"{k}={v}" for k, v in self._extra_cookies.items())
            cmd += ["-b", cookie_str]

        # Extra user-set headers (Referer already handled)
        for name, value in self._extra_headers.items():
            if name.lower() != "referer":
                cmd += ["-H", f"{name}: {value}"]

        # Write HTTP status code to stderr so we can capture it
        cmd += ["-w", "%{http_code}"]
        cmd.append(url)
        return cmd

    def _fetch(self, url: str) -> bytes | None:
        if not self._robots_ok(url):
            log.warning("Blocked by robots.txt: %s", url)
            return None
        self._throttle()

        output_path = Path(tempfile.mktemp(dir=self._tmp_dir, suffix=".bin"))
        try:
            cmd = self._build_command(url, output_path)
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=self.timeout + 10,
                check=False,
            )
            status_code = result.stdout.decode("ascii", errors="ignore").strip()

            if result.returncode != 0:
                stderr = result.stderr.decode("utf-8", errors="ignore").strip()
                log.warning("curl error for %s: %s (status=%s)",
                            url, stderr[:200], status_code)
                return None

            if status_code and not status_code.startswith("2") and \
               not status_code.startswith("3"):
                log.error("HTTP %s for %s", status_code, url)
                return None

            if not output_path.exists() or output_path.stat().st_size < 10:
                log.warning("Empty response for %s", url)
                return None

            return output_path.read_bytes()

        except subprocess.TimeoutExpired:
            log.warning("Timeout fetching %s", url)
            return None
        except Exception as e:  # noqa: BLE001
            log.warning("Fetch error for %s: %s", url, e)
            return None
        finally:
            try:
                output_path.unlink(missing_ok=True)
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False