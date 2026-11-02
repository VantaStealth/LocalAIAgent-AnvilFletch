"""Run Anvil Fletch's full self-test and scenario suite from the command line.

Usage:
    python scripts/selftest.py            # schemas, live control, scenarios
    python scripts/selftest.py --timing   # also time the hot control paths
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import agent_backend as ab  # noqa: E402


def timing():
    rows = []
    cases = [
        ("open_app", lambda: ab.t_open_app("AyuGram")),
        ("tray_icon_click", lambda: ab.t_tray_icon_click("AyuGram")),
        ("window_focus", lambda: ab.t_window_focus("AyuGram")),
        ("window_info", lambda: ab.t_window_info("AyuGram")),
        ("read_screen", lambda: ab.t_read_screen()),
        ("read_window", lambda: ab.t_window_text("AyuGram")),
        ("screenshot", lambda: ab.t_screenshot()),
    ]
    for name, fn in cases:
        t0 = time.time()
        try:
            ok, summary, _detail = fn()
        except Exception as e:
            ok, summary = False, f"{type(e).__name__}: {e}"
        rows.append((name, time.time() - t0, bool(ok), str(summary)[:80]))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timing", action="store_true", help="time the hot control paths")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    passed, failed, report = ab.run_self_test(verbose=args.verbose)
    print(report)
    print(f"\n{len(ab.SCENARIO_TESTS)} routing scenarios included")

    if args.timing:
        print("\nLATENCY (target: under 5s per action)")
        for name, secs, ok, summary in timing():
            print(f"  {'OK ' if ok else 'ERR'} {secs:6.2f}s  {name:<16} {summary}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
