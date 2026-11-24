"""List the variables and values of Statistics Denmark / Nationalbank StatBank tables.

Usage: python inspect_table.py DNRENTD DNRENTM DNVALD UHM NKN1
"""
import sys

import requests

for table in sys.argv[1:]:
    print(f"\n##### {table}")
    r = requests.post("https://api.statbank.dk/v1/tableinfo",
                      json={"table": table, "format": "JSON", "lang": "en"}, timeout=60)
    if r.status_code != 200:
        print("  not found:", r.status_code, r.text[:150])
        continue
    info = r.json()
    print(" ", info.get("text"))
    for v in info["variables"]:
        vals = v["values"]
        print(f"  - {v['id']} ({v['text']}){' [time]' if v.get('time') else ''}: {len(vals)} values")
        if not v.get("time"):
            for x in vals[:40]:
                print(f"      {x['id']!r}: {x['text']}")
