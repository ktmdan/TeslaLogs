"""Toolbox datatank client. All requests run inside the browser page via
``page.evaluate(fetch(...))`` so they ride the real browser network stack.
"""

from __future__ import annotations

import json
import time
from urllib.parse import quote

from playwright.sync_api import Page

from .auth import BASE_URL, page_fetch, page_fetch_many


class ToolboxError(RuntimeError):
    def __init__(self, status: int, url: str, body: str):
        super().__init__(f"GET {url} -> {status}: {body[:300]}")
        self.status = status
        self.url = url
        self.body = body


def _build_qs(params: dict) -> str:
    """Match the toolbox UI's query encoding: keep '=' and ':' literal in values."""
    parts = []
    for k, v in params.items():
        parts.append(f"{quote(str(k), safe='')}={quote(str(v), safe='=:')}")
    return "&".join(parts)


class ToolboxClient:
    def __init__(self, page: Page, vin: str):
        self.page = page
        self.vin = vin

    def _get(self, path: str, params: dict, retries: int = 3) -> dict:
        url = f"{BASE_URL}{path}?{_build_qs(params)}"
        attempt = 0
        while True:
            attempt += 1
            try:
                result = page_fetch(self.page, url)
            except Exception as e:  # noqa: BLE001
                if attempt > retries:
                    raise ToolboxError(0, url, str(e)) from e
                time.sleep(min(2 ** attempt, 8))
                continue

            status = result["status"]
            body = result["body"]
            if status in (429, 502, 503, 504) and attempt <= retries:
                time.sleep(min(2 ** attempt, 8))
                continue
            if status != 200:
                raise ToolboxError(status, url, body)
            try:
                return json.loads(body)
            except Exception as e:  # noqa: BLE001
                raise ToolboxError(status, url, f"non-JSON body: {e}") from e

    def list_signals(self, start_ms: int, end_ms: int) -> dict:
        return self._get(
            "/api/v3/datatank/signals",
            {
                "key": f"vin:{self.vin}",
                "start": start_ms,
                "end": end_ms,
                "includeAudience": "true",
                "expand": "true",
            },
        )

    def fetch_signal(self, sig_name: str, start_ms: int, end_ms: int) -> dict:
        return self._get(
            "/api/v3/datatank/signals",
            {
                "key": f"vin:{self.vin}",
                "filter": f"sig_name='{sig_name}'",
                "start": start_ms,
                "end": end_ms,
                "includeAudience": "true",
                "expand": "true",
            },
        )

    def _signal_url(self, sig_name: str, start_ms: int, end_ms: int) -> str:
        qs = _build_qs({
            "key": f"vin:{self.vin}",
            "filter": f"sig_name='{sig_name}'",
            "start": start_ms,
            "end": end_ms,
            "includeAudience": "true",
            "expand": "true",
        })
        return f"{BASE_URL}/api/v3/datatank/signals?{qs}"

    def fetch_signals_batch(
        self, sig_names: list[str], start_ms: int, end_ms: int, max_retries: int = 1,
    ) -> list[dict]:
        """Fetch N signals concurrently. Returns one result dict per name in
        the same order: {'status', 'body', 'parsed' (dict or None), 'error' (str or None)}.
        """
        urls = [self._signal_url(n, start_ms, end_ms) for n in sig_names]
        raw = page_fetch_many(self.page, urls)

        # Single retry pass for transient failures (429/5xx/network errors).
        for _ in range(max_retries):
            retry_idx = [
                i for i, r in enumerate(raw)
                if r["status"] in (0, 429, 502, 503, 504)
            ]
            if not retry_idx:
                break
            time.sleep(1)
            retry_urls = [urls[i] for i in retry_idx]
            retry_results = page_fetch_many(self.page, retry_urls)
            for orig_idx, new_r in zip(retry_idx, retry_results):
                if new_r["status"] == 200:
                    raw[orig_idx] = new_r

        results: list[dict] = []
        for r in raw:
            entry: dict = {"status": r["status"], "body": r["body"], "parsed": None, "error": None}
            if r["status"] != 200:
                entry["error"] = f"HTTP {r['status']}: {r['body'][:200]}"
            else:
                try:
                    entry["parsed"] = json.loads(r["body"])
                except Exception as e:  # noqa: BLE001
                    entry["error"] = f"non-JSON: {e}"
            results.append(entry)
        return results
