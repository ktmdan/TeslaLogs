"""CLI entry point.

Usage:
  python -m src.main \\
    --vin <YOUR_VIN> \\
    --start <START_MS_OR_ISO> \\
    --end   <END_MS_OR_ISO>
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .auth import toolbox_session, whoami
from .client import ToolboxClient
from .signals import fetch_all, list_signal_names, output_dir_for

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE = ROOT / "state" / "browser-profile"
DEFAULT_OUT = ROOT / "out"


def parse_time(value: str) -> int:
    v = value.strip()
    if v.isdigit():
        n = int(v)
        return n * 1000 if n < 10**12 else n
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tesla-toolbox-signals",
        description="Download Tesla toolbox datatank signals for a VIN/time range.",
    )
    p.add_argument("--vin", required=True, help="Vehicle VIN (17 chars).")
    p.add_argument("--start", required=True, help="Start time (ms epoch or ISO).")
    p.add_argument("--end", required=True, help="End time (ms epoch or ISO).")
    p.add_argument(
        "--out-dir", default=str(DEFAULT_OUT),
        help=f"Output base directory (default: {DEFAULT_OUT}).",
    )
    p.add_argument(
        "--profile-dir", default=str(DEFAULT_PROFILE),
        help=(
            "Persistent browser profile directory. Captcha + SSO state live here "
            f"so subsequent runs are non-interactive (default: {DEFAULT_PROFILE})."
        ),
    )
    p.add_argument(
        "--channel", default=None,
        help="Playwright channel: chrome, chrome-beta, msedge, etc. Overrides auto-detect.",
    )
    p.add_argument(
        "--executable-path", default=None,
        help="Full path to a Chromium-based browser binary (e.g. Brave). Overrides auto-detect.",
    )
    p.add_argument(
        "--login", action="store_true",
        help="Force the interactive login prompt even if the profile already has a session.",
    )
    p.add_argument(
        "--concurrency", type=int, default=10,
        help="Number of signal fetches to run in parallel inside the browser (default 10).",
    )
    p.add_argument(
        "--overwrite", action="store_true",
        help="Re-fetch even if the file exists.",
    )
    p.add_argument(
        "--only", nargs="*",
        help="Restrict to these signal names (skip catalog fetch).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="List signals and exit, do not fetch each one.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    start_ms = parse_time(args.start)
    end_ms = parse_time(args.end)
    if end_ms <= start_ms:
        print("error: --end must be greater than --start", file=sys.stderr)
        return 2

    profile_dir = Path(args.profile_dir)
    out_base = Path(args.out_dir)

    out_dir = output_dir_for(out_base, args.vin, start_ms, end_ms)
    print(f"[out] Writing to {out_dir}")

    with toolbox_session(
        profile_dir,
        channel=args.channel,
        executable_path=args.executable_path,
        force_login=args.login,
    ) as page:
        me = whoami(page)
        if me and me.get("user"):
            print(f"[auth] Logged in as {me['user'].get('email')}")

        client = ToolboxClient(page, args.vin)

        if args.only:
            names = sorted(set(args.only))
            print(f"[list] Using {len(names)} signal name(s) from --only")
        else:
            print(f"[list] Fetching catalog for {args.vin} ({start_ms}..{end_ms})")
            names = list_signal_names(client, start_ms, end_ms)
            print(f"[list] {len(names)} signals available")
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "_catalog.json").write_text(json.dumps(names, indent=2))

        if args.dry_run:
            print("[dry-run] Skipping per-signal fetches.")
            return 0

        counts = fetch_all(
            client, names, start_ms, end_ms, out_dir,
            concurrency=args.concurrency, overwrite=args.overwrite,
        )
    print(f"[done] {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
