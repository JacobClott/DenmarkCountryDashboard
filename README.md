# Denmark economy dashboard

Streamlit app with seven time-series charts (hover for exact values) and a Refresh button.

| Metric | Source |
|---|---|
| Real GDP growth (y/y, with q/q in hover) | Statistics Denmark `NKN1` (fallback: Eurostat `namq_10_gdp`) |
| Core inflation | Eurostat `prc_hicp_manr`, HICP excl. energy, food, alcohol & tobacco |
| 10Y government bond yield | Eurostat `irt_lt_mcby_m` (monthly, Maastricht-criterion yield). The Nationalbank's open StatBank has no current 10Y series |
| Gross government debt, % of GDP | Eurostat `gov_10q_ggdebt` (Maastricht debt; fallback annual `gov_10dd_edpt1`) |
| Nominal USD per DKK | Danmarks Nationalbank `DNVALD` (fallback: ECB reference rates) |
| Real USD per DKK | Derived: `S_t * P_DK / P_US`, OECD CPI (COICOP 2018 dataflow linked to COICOP 1999 history), index with Jan 1996 - Dec 2025 mean = 100 |
| Goods trade balance | Statistics Denmark `UHM` (exports - imports, DKK bn; SA or unadjusted) |

Real exchange rate: S = USD per DKK (monthly average of daily rates). Above 100 = krone stronger
(more "overvalued") in real terms than its 1996-2025 average; below 100 = weaker.

## Run locally
```
pip install -r requirements.txt
python validate_sources.py      # checks every table ID / series code against the live APIs
streamlit run app.py
```

## Deploy
1. Push this folder to a GitHub repo (`app.py` at the repo root).
2. On share.streamlit.io choose "New app", select the repo and `app.py`, deploy.
3. Data refreshes automatically when the cache expires (1h daily series, 6h monthly, 12h debt) or when someone clicks Refresh data.

## Notes
- Table IDs and variable labels were written from documentation, not live-tested. Run `validate_sources.py` first; the loaders discover StatBank variables by their English labels and print the available options when a label does not match.
- If a source fails, only that chart shows a warning; the rest still render.
