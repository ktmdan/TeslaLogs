"""Orchestrates: list signals -> fetch each -> write JSON files."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .client import ToolboxClient

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")


def _safe_filename(name: str) -> str:
    return _SAFE_NAME.sub("_", name)[:200] or "unnamed"


def _ts_label(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def output_dir_for(base: Path, vin: str, start_ms: int, end_ms: int) -> Path:
    folder = f"{_ts_label(start_ms)}_{_ts_label(end_ms)}"
    return base / vin / folder


def list_signal_names(client: ToolboxClient, start_ms: int, end_ms: int) -> list[str]:
    catalog = client.list_signals(start_ms, end_ms)
    if not isinstance(catalog, dict):
        raise RuntimeError(f"Unexpected catalog shape: {type(catalog).__name__}")
    return sorted(catalog.keys())


def fetch_all(
    client: ToolboxClient,
    sig_names: list[str],
    start_ms: int,
    end_ms: int,
    out_dir: Path,
    concurrency: int = 10,
    overwrite: bool = False,
    progress: bool = True,
) -> dict[str, int]:
    """Fetch signals in parallel batches and write one JSON per signal."""
    out_dir.mkdir(parents=True, exist_ok=True)
    counts = {"saved": 0, "skipped": 0, "empty": 0, "error": 0}

    to_fetch: list[str] = []
    for name in sig_names:
        dest = out_dir / f"{_safe_filename(name)}.json"
        if dest.exists() and not overwrite:
            counts["skipped"] += 1
        else:
            to_fetch.append(name)

    total = len(sig_names)
    done = counts["skipped"]
    batch_size = max(1, concurrency)

    if progress:
        print(f"[fetch] {len(to_fetch)} to fetch, {counts['skipped']} skipped, "
              f"concurrency={batch_size}")

    t0 = time.time()
    for i in range(0, len(to_fetch), batch_size):
        chunk = to_fetch[i:i + batch_size]
        results = client.fetch_signals_batch(chunk, start_ms, end_ms)
        for name, result in zip(chunk, results):
            done += 1
            if result["error"]:
                counts["error"] += 1
                if progress:
                    print(f"[{done}/{total}] ERROR  {name}: {result['error']}")
                continue
            payload = result["parsed"]
            dest = out_dir / f"{_safe_filename(name)}.json"
            dest.write_text(json.dumps(payload, indent=2, sort_keys=True))
            inner = payload.get(name) if isinstance(payload, dict) else None
            has_values = bool(inner and inner.get("timestamps"))
            if has_values:
                counts["saved"] += 1
                if progress:
                    print(f"[{done}/{total}] saved  {name}")
            else:
                counts["empty"] += 1
                if progress:
                    print(f"[{done}/{total}] empty  {name}")

    if progress:
        dt = time.time() - t0
        rate = len(to_fetch) / dt if dt > 0 else 0
        print(f"[fetch] done in {dt:.1f}s ({rate:.1f} signals/s)")
    return counts
