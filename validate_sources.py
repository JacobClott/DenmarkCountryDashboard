"""Check every data source against the live APIs.

Run on your own machine:   python validate_sources.py
Prints PASS/FAIL per metric with the latest value, date range, source and any fallback note.
"""
import sys
from datetime import datetime

import dk_sources as S

BOUNDS = {  # rough sanity ranges for the latest value
    "gdp": (-15, 15), "core": (-2, 15), "yield": (-1, 12), "debt": (10, 80),
    "fx": (0.05, 0.30), "rer": (50, 200), "trade": (-30, 60),
}


MAX_AGE_DAYS = {"daily": 10, "monthly": 120, "quarterly": 270, "annual": 550}


def main() -> int:
    failures = 0
    for m in S.METRICS:
        print(f"\n=== {m['title']} ===")
        try:
            s = m["loader"]()
            df = s.df
            last = df.iloc[-1]
            lo, hi = BOUNDS[m["key"]]
            ok = lo <= last["value"] <= hi
            age = (datetime.now() - last["date"].to_pydatetime()).days
            stale = age > MAX_AGE_DAYS[s.freq]
            status = "PASS" if ok and not stale else ("STALE" if stale else "CHECK (value outside expected range)")
            failures += 0 if (ok and not stale) else 1
            print(f"{status}: latest {last['value']:.4f} on {last['date']:%Y-%m-%d}; "
                  f"({age} days old); {len(df)} obs from {df['date'].min():%Y-%m-%d}; {s.freq}; source: {s.source}")
            if s.note:
                print(f"  note: {s.note}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL: {e}")
    print(f"\n{len(S.METRICS) - failures} of {len(S.METRICS)} metrics OK")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
