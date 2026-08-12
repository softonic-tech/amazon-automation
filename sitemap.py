"""
Sitemap Crawler
---------------
Discovers product URLs from a site's sitemap.xml.

Workflow:
  1. Fetch /robots.txt to find declared sitemap URLs.
  2. Fall back to /sitemap.xml if none declared.
  3. Recursively walk sitemap indexes → child sitemaps → URLs.
  4. Filter by a product URL regex (site-specific defaults included).
  5. Return N URLs (first-N or random sample).

Handles gzipped sitemaps (.xml.gz) transparently.
"""

from __future__ import annotations

import gzip
import logging
import random
import re
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

log = logging.getLogger("scraper.sitemap")

# XML namespace used in sitemap files
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


class SitemapCrawler:
    """
    Walks a site's XML sitemaps and returns product URLs.

    Product URL patterns per host — extend as needed:
      * zoro.com     → .../i/G1234567/
      * grainger.com → .../product/...
    """

    DEFAULT_PATTERNS: dict[str, str] = {
        "zoro.com":     r"/i/G\d+/?$",
        "grainger.com": r"/product/",
        "iherb.com":    r"/pr/[^/?#]+/\d+/?$",
    }

    def __init__(self, client, max_sitemaps: int = 20) -> None:
        """
        `client` must be an HttpClient (from scraper.py) with a get_bytes() method.
        `max_sitemaps` caps how many sitemap files we'll open in one run.
        """
        self.client = client
        self.max_sitemaps = max_sitemaps
        self._visited: set[str] = set()

    # ---- public API ------------------------------------------------------ #

    def collect_urls(
        self,
        base_url: str,
        limit: int = 25,
        pattern: str | None = None,
        sample_random: bool = False,
        pool_multiplier: int = 10,
    ) -> list[str]:
        """
        Return up to `limit` product URLs from the site.

        `pattern` — regex to filter URLs; defaults to a known pattern for the host.
        `sample_random` — if True, pull `limit` random URLs from the pool.
                          If False, return the first `limit` matches.
        `pool_multiplier` — when sampling randomly, collect this many × limit
                            candidates before sampling (for variety).
        """
        host = urlparse(base_url).netloc.replace("www.", "")
        pattern = pattern or self.DEFAULT_PATTERNS.get(host)
        if pattern is None:
            log.warning(
                "No default product-URL pattern for host %r. "
                "Pass --pattern to filter, or all sitemap URLs will be returned.",
                host,
            )
        regex = re.compile(pattern) if pattern else None

        target_pool = limit * pool_multiplier if sample_random else limit
        sitemap_urls = self._discover_sitemaps(base_url)
        log.info("Discovered %d top-level sitemap(s)", len(sitemap_urls))

        collected: list[str] = []
        for sm_url in sitemap_urls:
            if len(collected) >= target_pool:
                break
            collected.extend(self._walk(sm_url, regex, target_pool - len(collected)))

        # Deduplicate while preserving order
        seen: set[str] = set()
        unique: list[str] = []
        for u in collected:
            if u not in seen:
                seen.add(u)
                unique.append(u)

        log.info("Collected %d matching product URL(s) from sitemaps", len(unique))

        if sample_random and len(unique) > limit:
            return random.sample(unique, limit)
        return unique[:limit]

    # ---- internals ------------------------------------------------------- #

    def _discover_sitemaps(self, base_url: str) -> list[str]:
        """Look for Sitemap: entries in robots.txt; fall back to /sitemap.xml."""
        robots_url = urljoin(base_url, "/robots.txt")
        text = self.client.get(robots_url)  # returns str
        sitemaps: list[str] = []
        if text:
            for line in text.splitlines():
                line = line.strip()
                if line.lower().startswith("sitemap:"):
                    sitemaps.append(line.split(":", 1)[1].strip())
        if not sitemaps:
            sitemaps.append(urljoin(base_url, "/sitemap.xml"))
        return sitemaps

    def _walk(
        self,
        url: str,
        regex: re.Pattern | None,
        need: int,
        depth: int = 0,
    ) -> list[str]:
        """
        Recurse through a sitemap or sitemap index.
        Returns matching URLs, stopping once we've collected `need`.
        """
        if depth > 5 or url in self._visited:
            return []
        if len(self._visited) >= self.max_sitemaps:
            log.debug("max_sitemaps reached; stopping recursion")
            return []
        self._visited.add(url)

        content = self._fetch_sitemap(url)
        if content is None:
            return []

        try:
            root = ET.fromstring(content)
        except ET.ParseError as e:
            log.warning("XML parse error for %s: %s", url, e)
            return []

        tag = root.tag.lower()
        found: list[str] = []

        if tag.endswith("sitemapindex"):
            # Prefer sitemaps that mention "product" in the URL — big speed win
            child_urls = [
                loc.text.strip()
                for loc in root.findall("sm:sitemap/sm:loc", SITEMAP_NS)
                if loc.text
            ]
            child_urls.sort(key=lambda u: 0 if "product" in u.lower() else 1)

            for child in child_urls:
                if len(found) >= need:
                    break
                found.extend(self._walk(child, regex, need - len(found), depth + 1))

        elif tag.endswith("urlset"):
            for loc in root.findall("sm:url/sm:loc", SITEMAP_NS):
                if loc.text is None:
                    continue
                u = loc.text.strip()
                if regex is None or regex.search(u):
                    found.append(u)
                    if len(found) >= need:
                        break

        else:
            log.warning("Unexpected root tag %r in %s", root.tag, url)

        return found

    def _fetch_sitemap(self, url: str) -> bytes | None:
        """Fetch a sitemap; transparently decompress if it's .xml.gz."""
        data = self.client.get_bytes(url)
        if data is None:
            return None
        # .xml.gz is content-level gzip, not transport-level — requests won't
        # auto-decode it. Also handle servers that serve .gz already decoded.
        if url.endswith(".gz"):
            try:
                data = gzip.decompress(data)
            except (gzip.BadGzipFile, OSError):
                pass  # already decompressed
        return data