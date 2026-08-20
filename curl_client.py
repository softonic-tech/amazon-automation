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
import os
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

# curl-impersonate wrapper scripts (v0.x style) to try in preference order.
# The v1.x line ships a single `curl-impersonate` binary that takes
# `--impersonate <profile>`; we detect that separately below.
IMPERSONATE_WRAPPERS = (
    "curl_chrome136", "curl_chrome131", "curl_chrome124",
    "curl_chrome120", "curl_chrome116",
    "curl_ff135", "curl_ff117",
)
IMPERSONATE_PROFILE = os.environ.get("CURL_IMPERSONATE_PROFILE", "chrome131")


def _detect_curl_binary() -> tuple[str, list[str], bool]:
    """Prefer curl-impersonate if installed — its Chrome/Firefox TLS
    fingerprint beats Cloudflare Bot Fight Mode from datacenter IPs where
    system curl gets 403'd. Falls back to system curl.

    Returns (binary_path, extra_args_before_url, is_impersonating).
    """
    single = shutil.which("curl-impersonate") or shutil.which("curl_impersonate")
    if single:
        return single, ["--impersonate", IMPERSONATE_PROFILE], True
    for name in IMPERSONATE_WRAPPERS:
        p = shutil.which(name)
        if p:
            return p, [], True
    p = shutil.which("curl") or shutil.which("curl.exe")
    if not p:
        raise RuntimeError(
            "No curl binary found on PATH. Install curl "
            "(apt install curl) or curl-impersonate "
            "(https://github.com/lexiforest/curl-impersonate)."
        )
    return p, [], False


class CurlClient:
    """
    HTTP client that shells out to curl for each request. Retains the same
    interface as HttpClient so it drops in cleanly.

    Session state (cookies) is persisted via curl's cookie jar file so that
    Cloudflare's clearance cookies stick across requests.

    Auto-detects curl-impersonate when installed and uses it (needed to beat
    Cloudflare Bot Fight from datacenter IPs on sites like iHerb). Also reads
    an optional `HTTP_PROXY_URL` env var to route requests through a
    residential proxy (fallback if IP reputation alone still 403s).
    """

    def __init__(
        self,
        delay: float = 2.0,
        timeout: int = 30,
        respect_robots: bool = True,
        proxy: str | None = None,
        use_env_proxy: bool = True,
    ) -> None:
        self._curl, self._impersonate_args, self._impersonating = _detect_curl_binary()

        try:
            v = subprocess.run(
                [self._curl, "--version"],
                capture_output=True, timeout=5, check=True, text=True,
            )
            mode = "impersonate" if self._impersonating else "system"
            log.info("curl ready (%s): %s", mode, v.stdout.splitlines()[0])
        except (subprocess.SubprocessError, FileNotFoundError) as e:
            raise RuntimeError(f"curl exists but failed to run: {e}") from e

        self.delay = delay
        self.timeout = timeout
        self.respect_robots = respect_robots
        # Explicit `proxy=` arg wins. `use_env_proxy=False` skips the env
        # fallback — needed for Zoro, whose Akamai edge treats Cloudflare
        # WARP exit IPs as bots even when TLS impersonation succeeds.
        if proxy:
            self._proxy = proxy
        elif use_env_proxy:
            self._proxy = (
                os.environ.get("HTTP_PROXY_URL")
                or os.environ.get("HTTPS_PROXY")
                or os.environ.get("HTTP_PROXY")
                or None
            )
        else:
            self._proxy = None
        if self._proxy:
            log.info("curl proxy in use (host redacted)")
        else:
            log.info("curl proxy not set (HTTP_PROXY_URL empty) — "
                     "datacenter IP will be used")

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

    def _build_command(
        self,
        url: str,
        output_path: Path,
        proxy: str | None = None,
    ) -> list[str]:
        parsed = urlparse(url)
        cmd: list[str] = [
            self._curl,
            *self._impersonate_args,   # e.g. ["--impersonate", "chrome131"]
            "-L",                      # follow redirects
            "--compressed",            # accept gzip/br transport encoding
            "--silent",
            "--show-error",
            "-o", str(output_path),
            "-c", str(self._cookie_jar),   # write cookies back
            "--max-time", str(self.timeout),
        ]
        # Only *read* the jar once it exists. Passing -b on a missing file
        # can make curl send a blank Cookie header, which Akamai flags.
        if self._cookie_jar.exists():
            cmd += ["-b", str(self._cookie_jar)]

        # curl-impersonate injects its own UA, Accept, Accept-Language,
        # sec-ch-*, sec-fetch-* headers to match the real browser it mimics.
        # Extra -H flags (including Referer) change header order and can
        # turn a 301 into a 403 on Akamai. Only add them for plain curl.
        if not self._impersonating:
            default_referer = f"{parsed.scheme}://{parsed.netloc}/"
            referer = self._extra_headers.get("Referer", default_referer)
            cmd += [
                "-A", DEFAULT_UA,
                "-H", ("Accept: text/html,application/xhtml+xml,"
                       "application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"),
                "-H", "Accept-Language: en-US,en;q=0.9",
                "-H", "Cache-Control: no-cache",
                "-H", f"Referer: {referer}",
            ]

        # Route through a proxy (e.g. residential IP pool) when configured.
        use_proxy = proxy if proxy is not None else self._proxy
        if use_proxy:
            cmd += ["-x", use_proxy]

        # Inline cookies from set_cookie() — combines with jar
        if self._extra_cookies:
            cookie_str = "; ".join(f"{k}={v}" for k, v in self._extra_cookies.items())
            cmd += ["-b", cookie_str]

        # Extra user-set headers. Skip when impersonating — they break the
        # fingerprint (iHerb cookies still go via -b, which is fine).
        if not self._impersonating:
            for name, value in self._extra_headers.items():
                if name.lower() != "referer":
                    cmd += ["-H", f"{name}: {value}"]

        # Write HTTP status code to stdout so we can capture it
        cmd += ["-w", "%{http_code}"]
        cmd.append(url)
        return cmd

    def _run_once(self, url: str, output_path: Path, proxy: str | None) -> tuple[str, bytes | None]:
        """Run one curl request. Returns (status_code, body_or_none)."""
        cmd = self._build_command(url, output_path, proxy=proxy)
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
            return status_code, None

        if status_code and not status_code.startswith("2") and \
           not status_code.startswith("3"):
            log.error("HTTP %s for %s%s",
                      status_code, url,
                      " (via proxy)" if proxy else " (direct)")
            return status_code, None

        if not output_path.exists() or output_path.stat().st_size < 10:
            log.warning("Empty response for %s", url)
            return status_code, None

        return status_code, output_path.read_bytes()

    def _fetch(self, url: str) -> bytes | None:
        if not self._robots_ok(url):
            log.warning("Blocked by robots.txt: %s", url)
            return None
        self._throttle()

        output_path = Path(tempfile.mktemp(dir=self._tmp_dir, suffix=".bin"))
        try:
            status, body = self._run_once(url, output_path, proxy=self._proxy)
            # Direct datacenter IP blocked (typical Akamai 403). Retry once
            # through WARP / HTTP_PROXY_URL if we weren't already proxied.
            if body is None and status in {"403", "429", "503"} and not self._proxy:
                fallback = (
                    os.environ.get("HTTP_PROXY_URL")
                    or os.environ.get("HTTPS_PROXY")
                    or os.environ.get("HTTP_PROXY")
                    or None
                )
                if fallback:
                    log.warning(
                        "Retrying %s via proxy after HTTP %s", url, status,
                    )
                    try:
                        output_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    _, body = self._run_once(url, output_path, proxy=fallback)
            return body

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