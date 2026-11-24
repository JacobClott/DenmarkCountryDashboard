"""Data loaders for the Denmark economy dashboard.

Pure Python (no Streamlit import) so it can be used by app.py *and* by
validate_sources.py.

Every loader returns a `Series` whose `.df` has at least the columns
`date` (pandas Timestamp) and `value` (float), sorted by date.

Primary sources
---------------
* Statistics Denmark (DST) StatBank API  - GDP, trade (and the Nationalbank tables,
  which are published in the same StatBank)
* Danmarks Nationalbank (via StatBank)   - USD/DKK, 10Y government bond yield
* Eurostat                               - core HICP inflation, general government debt
* OECD                                   - CPI for Denmark and the US (real exchange rate)
* ECB                                    - fallback for the exchange rate
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd
import requests

TIMEOUT = 45
HEADERS = {"User-Agent": "denmark-economy-dashboard/1.0"}

# Real exchange rate base window (fixed, so history never revises)
RER_BASE_START = "1996-01-01"
RER_BASE_END = "2025-12-01"
RER_BASE_MONTHS = 360
MAX_MISSING_BASE_MONTHS = 6  # tolerate a few months with no CPI; they are left out of the mean


class DataError(Exception):
    """Raised when a source cannot be fetched or does not look as expected."""


@dataclass
class Series:
    df: pd.DataFrame
    source: str
    freq: str  # "daily" | "monthly" | "quarterly" | "annual"
    note: str = ""
    extra: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Generic helpers
# --------------------------------------------------------------------------- #
def _request(method: str, url: str, timeout: int = TIMEOUT, attempts: int = 2, **kw) -> requests.Response:
    last = None
    for _ in range(attempts):
        try:
            r = requests.request(method, url, timeout=timeout, headers=HEADERS, **kw)
        except requests.RequestException as e:  # network / DNS / timeout -> retry once
            last = DataError(f"Network error for {url}: {e}")
            continue
        if r.status_code >= 500:
            last = DataError(f"HTTP {r.status_code} from {url}: {r.text[:300]!r}")
            continue
        if r.status_code >= 400:
            raise DataError(f"HTTP {r.status_code} from {url}: {r.text[:300]!r}")
        return r
    raise last


def parse_period(s) -> pd.Timestamp:
    """Parse DST / Eurostat / OECD period strings to the period's first day."""
    s = str(s).strip()
    pats = [
        (r"(\d{4})M(\d{2})D(\d{2})", lambda m: (m[1], m[2], m[3])),  # 2026M10D29
        (r"(\d{4})-(\d{2})-(\d{2})", lambda m: (m[1], m[2], m[3])),  # 2026-10-29
        (r"(\d{4})M(\d{2})", lambda m: (m[1], m[2], "01")),  # 2026M10
        (r"(\d{4})-(\d{2})", lambda m: (m[1], m[2], "01")),  # 2026-10
        (r"(\d{4})-?Q(\d)", lambda m: (m[1], f"{3 * (int(m[2]) - 1) + 1:02d}", "01")),
        (r"(\d{4})", lambda m: (m[1], "01", "01")),  # 2026
    ]
    for pat, fn in pats:
        m = re.fullmatch(pat, s)
        if m:
            y, mo, d = fn(m)
            return pd.Timestamp(int(y), int(mo), int(d))
    raise DataError(f"Unrecognised period format: {s!r}")


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.dropna(subset=["value"]).sort_values("date").drop_duplicates("date", keep="last")
    return df.reset_index(drop=True)


def first_success(attempts: list[tuple[str, Callable[[], Series]]], max_age_days: int | None = None) -> Series:
    """Try loaders in order; return the first that works (and is fresh enough).

    Earlier failures are recorded in the returned series' note.
    """
    errors = []
    for label, fn in attempts:
        try:
            s = fn()
            if max_age_days is not None:
                age = (pd.Timestamp.now() - s.df["date"].iloc[-1]).days
                if age > max_age_days:
                    raise DataError(f"latest observation is {age} days old")
            if errors:
                s.note = (s.note + " " if s.note else "") + "Fallback in use - " + "; ".join(errors)
            return s
        except Exception as e:  # noqa: BLE001 - we want to try the next source
            errors.append(f"{label} failed ({str(e)[:1200]})")
    raise DataError(" | ".join(errors))


def _infer_freq(df: pd.DataFrame) -> str:
    step = df["date"].diff().dt.days.median()
    return "daily" if step <= 5 else "monthly" if step <= 40 else "quarterly" if step <= 100 else "annual"


# --------------------------------------------------------------------------- #
# Statistics Denmark StatBank client (also serves Nationalbank tables)
# --------------------------------------------------------------------------- #
DST_API = "https://api.statbank.dk/v1"


def match(*terms: str, exclude: tuple[str, ...] = (), many: bool = False, fallback_first: bool = False):
    """Value selector: pick StatBank values whose text contains all `terms`."""

    def sel(values):
        out = [
            v["id"]
            for v in values
            if all(t.lower() in v["text"].lower() for t in terms)
            and not any(x.lower() in v["text"].lower() for x in exclude)
        ]
        if not out and fallback_first:
            out = [values[0]["id"]]
        return out if many else out[:1]

    return sel


def select_all(values):
    return [v["id"] for v in values]


def dst_fetch(table: str, picks: dict[str, Callable]) -> tuple[pd.DataFrame, dict[str, str]]:
    """Fetch a StatBank table.

    `picks` maps a (lower-case) fragment of a variable's English label to a value selector.
    Variables not mentioned are eliminated if the table allows it, otherwise the first
    value is used. Returns (df, cols) where df has one column per requested variable (value
    labels as text), plus `date` and `value`, and `cols` maps each pick key to its column.
    """
    info = _request("POST", f"{DST_API}/tableinfo", json={"table": table, "format": "JSON", "lang": "en"}).json()
    req, cols, time_id = [], {}, None
    unused = set(picks)
    text_to_id = {}
    for v in info["variables"]:
        vid, vtext, vals = v["id"], v["text"], v["values"]
        text_to_id[vtext] = vid
        if v.get("time"):
            req.append({"code": vid, "values": ["*"]})
            time_id = vid
            continue
        key = next((k for k in picks if k.lower() in vtext.lower()), None)
        if key is not None:
            ids = picks[key](vals)
            if not ids:
                opts = [x["text"] for x in vals][:30]
                raise DataError(f"{table}: no value matched for '{key}' (variable '{vtext}'). Options: {opts}")
            req.append({"code": vid, "values": ids})
            cols[key] = vid
            unused.discard(key)
        elif v.get("elimination"):
            continue
        else:
            req.append({"code": vid, "values": [vals[0]["id"]]})
    if unused:
        raise DataError(f"{table}: variables not found for {sorted(unused)}. Table variables: {list(text_to_id)}")
    if time_id is None:
        raise DataError(f"{table}: no time variable found")

    r = _request(
        "POST",
        f"{DST_API}/data",
        json={"table": table, "format": "CSV", "lang": "en", "delimiter": "Semicolon", "variables": req},
        timeout=180,
    )
    df = pd.read_csv(io.StringIO(r.content.decode("utf-8-sig")), sep=";", na_values=["..", "-", ""])
    if df.shape[1] < 2:
        raise DataError(f"{table}: unexpected response: {r.text[:200]!r}")
    ids = [v["code"] for v in req]
    if df.shape[1] == len(ids) + 1:  # columns come in request order, last one is the value
        df.columns = ids + ["value"]
    else:
        df = df.rename(columns=text_to_id)
        df = df.rename(columns={df.columns[-1]: "value"})
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df["date"] = df[time_id].map(parse_period)
    return df, cols


# --------------------------------------------------------------------------- #
# Eurostat client (JSON-stat)
# --------------------------------------------------------------------------- #
EUROSTAT_API = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"


def eurostat(dataset: str, **params) -> pd.DataFrame:
    """Fetch a single Eurostat series (all non-time dimensions must resolve to one value)."""
    r = _request("GET", f"{EUROSTAT_API}/{dataset}", params={**params, "format": "JSON", "lang": "EN"})
    j = r.json()
    ids, sizes = j["id"], j["size"]
    multi = [i for i, s in zip(ids, sizes) if i != "time" and s > 1]
    if multi:
        raise DataError(f"Eurostat {dataset}: parameters left {multi} ambiguous; add filters")
    labels = j["dimension"]["time"]["category"]["index"]  # {"2025-10": 0, ...}
    vals = j.get("value", {})
    if isinstance(vals, list):
        vals = {str(i): v for i, v in enumerate(vals) if v is not None}
    rows = [(parse_period(lab), vals[str(pos)]) for lab, pos in labels.items() if str(pos) in vals]
    if not rows:
        raise DataError(f"Eurostat {dataset}: no observations returned for {params}")
    return _clean(pd.DataFrame(rows, columns=["date", "value"]))


# --------------------------------------------------------------------------- #
# 1. Real GDP growth  (Statistics Denmark, quarterly national accounts)
# --------------------------------------------------------------------------- #
def load_gdp_growth() -> Series:
    def dst():
        df, _ = dst_fetch(
            "NKN1",
            {
                "transaction": match("gross domestic product", exclude=("per capita",)),
                "price": match("chain", exclude=("index", "growth"), fallback_first=False),
                "seasonal": match("seasonally adjusted", exclude=("not ", "non", "unadjusted")),
            },
        )
        lvl = _clean(df[["date", "value"]])
        out = lvl.copy()
        out["value"] = lvl["value"].pct_change(4) * 100  # y/y
        out["qoq"] = lvl["value"].pct_change(1) * 100
        out["level"] = lvl["value"]
        return Series(_clean(out).dropna(subset=["value"]), "Statistics Denmark (NKN1)", "quarterly",
                      "Seasonally adjusted, chain-linked volumes; growth computed from levels.")

    def eurostat_fallback():
        lvl = eurostat("namq_10_gdp", freq="Q", unit="CLV_I20", s_adj="SCA", na_item="B1GQ", geo="DK")
        out = lvl.copy()
        out["value"] = lvl["value"].pct_change(4) * 100
        out["qoq"] = lvl["value"].pct_change(1) * 100
        return Series(out.dropna(subset=["value"]), "Eurostat (namq_10_gdp)", "quarterly",
                      "Seasonally adjusted, chain-linked volumes; growth computed from levels.")

    return first_success([("Statistics Denmark NKN1", dst), ("Eurostat namq_10_gdp", eurostat_fallback)])


# --------------------------------------------------------------------------- #
# 2. Core inflation  (Eurostat HICP excl. energy, food, alcohol & tobacco, y/y)
# --------------------------------------------------------------------------- #
def _eurostat_json(dataset: str, **params) -> dict:
    return _request("GET", f"{EUROSTAT_API}/{dataset}", params={**params, "format": "JSON", "lang": "EN"}).json()


def _find_core_code(dataset: str) -> tuple[str, str, str]:
    """Find the dimension id and code for 'all-items excl. energy, food, alcohol and tobacco'.

    Eurostat re-coded the HICP in Jan 2026 (ECOICOP v2), so look the code up by its label instead
    of hard-coding it. Returns (dimension_id, code, label).
    """
    j = _eurostat_json(dataset, freq="M", unit="RCH_A", geo="DK", lastTimePeriod=1)
    dim = next((i for i in j["id"] if i.startswith("coicop")), None)
    if dim is None:
        raise DataError(f"{dataset}: no coicop dimension found in {j['id']}")
    labels = j["dimension"][dim]["category"]["label"]
    need = ("excluding", "energy", "food", "alcohol", "tobacco")
    bad = ("unprocessed", "non-energy", "services", "processed", "goods")
    hits = {c: l for c, l in labels.items()
            if all(w in l.lower() for w in need) and not any(b in l.lower() for b in bad)}
    if not hits:
        raise DataError(f"{dataset}: no core-inflation category found among {len(labels)} labels")
    code = min(hits, key=lambda c: len(hits[c]))
    return dim, code, hits[code]


def load_core_inflation() -> Series:
    # ECOICOP v2 dataset (from Jan 2026, full history). The old prc_hicp_manr stops at Dec 2025.
    def v2(dataset):
        def run():
            dim, code, label = _find_core_code(dataset)
            df = eurostat(dataset, freq="M", unit="RCH_A", geo="DK", **{dim: code})
            return Series(df, f"Eurostat ({dataset}, {code})", "monthly",
                          f"HICP '{label}'; annual rate of change.")
        return run

    return first_success([("Eurostat prc_hicp_minr", v2("prc_hicp_minr")),
                          ("Eurostat prc_hicp_manr", v2("prc_hicp_manr"))])


# --------------------------------------------------------------------------- #
# 3. 10-year government bond yield  (Nationalbank daily -> monthly -> Eurostat)
# --------------------------------------------------------------------------- #
def load_bond_yield() -> Series:
    # The Nationalbank's open StatBank has no current 10-year yield (DNRENTD has none; the 10-year in
    # DNRENTM ends 2012; MPK22 ends 2018), so use Eurostat's monthly long-term (Maastricht-criterion) yield.
    df = eurostat("irt_lt_mcby_m", freq="M", int_rt="MCBY", geo="DK")
    return Series(df, "Eurostat (irt_lt_mcby_m, Maastricht-criterion 10Y yield)", "monthly",
                  "Monthly average of the 10-year government bond yield. No current daily series is available from primary sources.")


# --------------------------------------------------------------------------- #
# 4. Gross general government debt, % of GDP  (Eurostat, Maastricht debt)
# --------------------------------------------------------------------------- #
def load_gov_debt() -> Series:
    return first_success(
        [
            ("Eurostat gov_10q_ggdebt", lambda: Series(
                eurostat("gov_10q_ggdebt", freq="Q", unit="PC_GDP", sector="S13", na_item="GD", geo="DK"),
                "Eurostat (gov_10q_ggdebt)", "quarterly", "Maastricht (EDP) gross debt, general government.")),
            ("Eurostat gov_10dd_edpt1", lambda: Series(
                eurostat("gov_10dd_edpt1", freq="A", unit="PC_GDP", sector="S13", na_item="GD", geo="DK"),
                "Eurostat (gov_10dd_edpt1)", "annual", "Maastricht (EDP) gross debt, general government.")),
        ]
    )


# --------------------------------------------------------------------------- #
# 5. Nominal exchange rate: USD per 1 DKK  (Nationalbank daily; ECB fallback)
# --------------------------------------------------------------------------- #
def _dn_fx() -> Series:
    def is_usd(values):
        out = [v["id"] for v in values if v["id"].upper() == "USD" or "(usd)" in v["text"].lower()
               or "us dollar" in v["text"].lower() or "u.s. dollar" in v["text"].lower()]
        return out[:1]

    df, _ = dst_fetch(
        "DNVALD",
        {
            "currency": is_usd,
            # Prefer the plain "DKK per 100 units" quote if the table offers several types
            "type": match("100", fallback_first=True),
        },
    )
    s = _clean(df[["date", "value"]])
    s = s[s["value"] > 0]  # the table can hold zeros on non-trading days; they are not rates
    dkk_per_usd = s["value"] / 100 if s["value"].median() > 50 else s["value"]  # Nationalbank quotes per 100 units
    out = pd.DataFrame({"date": s["date"], "value": 1 / dkk_per_usd})
    return Series(_clean(out), "Danmarks Nationalbank (DNVALD)", "daily",
                  "USD per 1 DKK (inverted from the Nationalbank's DKK-per-USD quote).")


def _ecb_fx() -> Series:
    def ecb(key):
        r = _request("GET", f"https://data-api.ecb.europa.eu/service/data/EXR/{key}",
                     params={"format": "csvdata", "startPeriod": "1999-01-01"})
        d = pd.read_csv(io.StringIO(r.text))
        d = pd.DataFrame({"date": pd.to_datetime(d["TIME_PERIOD"]), "value": pd.to_numeric(d["OBS_VALUE"], errors="coerce")})
        return _clean(d).set_index("date")["value"]

    usd_per_eur, dkk_per_eur = ecb("D.USD.EUR.SP00.A"), ecb("D.DKK.EUR.SP00.A")
    out = (usd_per_eur / dkk_per_eur).dropna().rename("value").reset_index()
    return Series(_clean(out), "ECB euro reference rates (cross rate)", "daily",
                  "USD per 1 DKK via EUR cross; history starts 1999.")


def load_fx_nominal() -> Series:
    return first_success([("Nationalbank DNVALD", _dn_fx), ("ECB reference rates", _ecb_fx)])


# --------------------------------------------------------------------------- #
# 6. Real exchange rate  (derived: S * P_DK / P_US, indexed to 1996-2025 mean = 100)
# --------------------------------------------------------------------------- #
OECD_C99 = "https://sdmx.oecd.org/public/rest/data/OECD.SDD.TPS,DSD_PRICES@DF_PRICES_ALL,1.0"
OECD_C18 = "https://sdmx.oecd.org/public/rest/data/OECD.SDD.TPS,DSD_PRICES_COICOP2018@DF_PRICES_C2018_ALL,1.0"
OECD_KEY = "DNK+USA.M.N.CPI.IX._T.N._Z"  # area.freq.methodology.measure.unit.expenditure.adjustment.transformation


def _oecd_flow(base: str) -> pd.DataFrame:
    """Monthly all-items CPI index (columns DNK, USA) from one OECD dataflow."""

    def get(key):
        r = _request("GET", f"{base}/{key}", params={"startPeriod": "1995-01", "format": "csvfilewithlabels"})
        return pd.read_csv(io.StringIO(r.text))

    try:
        d = get(OECD_KEY)
    except DataError:
        d = get("DNK+USA.M......")  # wildcard, then filter below
        for col, want in [("MEASURE", "CPI"), ("UNIT_MEASURE", "IX"), ("EXPENDITURE", "_T"),
                          ("METHODOLOGY", "N"), ("ADJUSTMENT", "N"), ("TRANSFORMATION", "_Z")]:
            if col in d.columns:
                d = d[d[col] == want]
    if d.empty:
        raise DataError("OECD CPI query returned no rows")
    d = d.copy()
    d["OBS_VALUE"] = pd.to_numeric(d["OBS_VALUE"], errors="coerce")
    d.loc[d["OBS_VALUE"] <= 0, "OBS_VALUE"] = np.nan  # a CPI index can never be zero or negative
    d = d.dropna(subset=["OBS_VALUE"])
    dup = d[d.duplicated(["REF_AREA", "TIME_PERIOD"], keep=False)]
    if not dup.empty and (dup.groupby(["REF_AREA", "TIME_PERIOD"])["OBS_VALUE"].nunique() > 1).any():
        raise DataError("OECD CPI: several different series share the same area and month")
    d = d.drop_duplicates(["REF_AREA", "TIME_PERIOD"])
    d["date"] = pd.to_datetime(d["TIME_PERIOD"])
    piv = d.pivot(index="date", columns="REF_AREA", values="OBS_VALUE").sort_index()
    return piv.reindex(columns=["DNK", "USA"])  # a country the dataflow lacks becomes an all-NaN column


def _link(new: pd.Series, old: pd.Series) -> pd.Series:
    """Extend `new` backwards with `old`, rescaled over the first 12 overlapping months."""
    both = new.dropna().index.intersection(old.dropna().index)
    if len(both) == 0:
        raise DataError("OECD COICOP 1999 and 2018 CPI series do not overlap; cannot link them")
    k = (new[both] / old[both]).iloc[:12].mean()
    return new.combine_first(old * k)


def load_oecd_cpi() -> pd.DataFrame:
    """Monthly all-items CPI for Denmark and the US.

    Each country comes from whichever OECD dataflow has it. Denmark has moved to COICOP 2018 (newer data),
    the US has not; where both flows hold a country they are linked over their overlap.
    """
    errs, flows = [], {}
    for label, base in (("COICOP 2018", OECD_C18), ("COICOP 1999", OECD_C99)):
        try:
            flows[label] = _oecd_flow(base)
        except DataError as e:
            errs.append(f"{label}: {e}")
    if not flows:
        raise DataError("; ".join(errs))
    new, old = flows.get("COICOP 2018"), flows.get("COICOP 1999")
    idx = pd.DatetimeIndex(sorted(set().union(*[f.index for f in flows.values()])))
    cols, used = {}, []
    for c in ("DNK", "USA"):
        n = new[c].reindex(idx) if new is not None and new[c].notna().any() else None
        o = old[c].reindex(idx) if old is not None and old[c].notna().any() else None
        if n is not None and o is not None:
            cols[c] = _link(n, o)
            used.append(f"{c}: COICOP 2018 linked to 1999")
        elif n is not None or o is not None:
            cols[c] = n if n is not None else o
            used.append(f"{c}: {'COICOP 2018' if n is not None else 'COICOP 1999'} only")
        else:
            raise DataError(f"OECD CPI: no data for {c}. {'; '.join(errs)}")
    out = pd.DataFrame(cols)
    out.attrs["flows"] = ", ".join(used) + (f" [{'; '.join(errs)}]" if errs else "")
    win = pd.date_range(RER_BASE_START, RER_BASE_END, freq="MS")
    out.attrs["coverage"] = "; ".join(
        f"{c}: {out[c].dropna().index.min():%b %Y}-{out[c].dropna().index.max():%b %Y}, "
        f"{int(out[c].reindex(win).isna().sum())} of 360 base months missing" for c in ("DNK", "USA"))
    return out


def compute_rer(usd_per_dkk_daily: pd.DataFrame, cpi: pd.DataFrame) -> pd.DataFrame:
    """RER_t = 100 * (S_t * P_DK/P_US) / mean(same quantity over Jan-1996..Dec-2025).

    S_t is USD per DKK (monthly average of daily rates). Above 100 = krone stronger / more
    "overvalued" in real terms than its 1996-2025 average.
    """
    daily = usd_per_dkk_daily[np.isfinite(usd_per_dkk_daily["value"]) & (usd_per_dkk_daily["value"] > 0)]
    s = daily.set_index("date")["value"].resample("MS").mean().rename("s")
    df = pd.concat([s, cpi], axis=1).dropna()
    df = df[(df["DNK"] > 0) & (df["USA"] > 0) & (df["s"] > 0)]
    raw = df["s"] * df["DNK"] / df["USA"]
    base = raw.loc[RER_BASE_START:RER_BASE_END]
    if len(base) < RER_BASE_MONTHS - MAX_MISSING_BASE_MONTHS:
        raise DataError(
            f"Real exchange rate base window incomplete: {len(base)} of {RER_BASE_MONTHS} months "
            f"(needs exchange rate and CPI data for Jan 1996 - Dec 2025)."
        )
    out = pd.DataFrame({"date": raw.index, "value": 100 * raw.values / base.mean()})
    out["nominal"] = df["s"].values
    out["cpi_ratio"] = (df["DNK"] / df["USA"]).values
    out = _clean(out)
    # Months with no CPI (e.g. Oct 2025, when the US published none) are excluded from the base-period
    # mean and left as gaps in the series rather than filled.
    full = pd.date_range(out["date"].min(), out["date"].max(), freq="MS")
    missing = [d for d in full if d not in set(out["date"])]
    out = out.set_index("date").reindex(full).rename_axis("date").reset_index()
    out.attrs["excluded"] = [f"{d:%b %Y}" for d in missing]
    return out


def load_fx_real() -> Series:
    fx = load_fx_nominal()
    cpi = load_oecd_cpi()
    df = compute_rer(fx.df, cpi)
    note = "Derived: USD-per-DKK x (Danish CPI / US CPI), indexed so the Jan 1996 - Dec 2025 average = 100. OECD CPI."
    note += f" CPI source: {cpi.attrs.get('flows', 'OECD')} ({cpi.attrs.get('coverage', '')})."
    if df.attrs.get("excluded"):
        note += " No CPI available for " + ", ".join(df.attrs["excluded"]) + ": excluded from the average and shown as a gap."
    if fx.note:
        note += " Exchange-rate note: " + fx.note
    return Series(df, "Derived from Nationalbank FX and OECD CPI", "monthly", note)


# --------------------------------------------------------------------------- #
# 7. Goods trade balance  (Statistics Denmark UHM, exports - imports)
# --------------------------------------------------------------------------- #
def _world_or_first(values):
    hit = [v["id"] for v in values if any(k in v["text"].lower() for k in ("world", "all countries", "total"))]
    return (hit or [values[0]["id"]])[:1]


def load_trade_balance() -> Series:
    df, cols = dst_fetch(
        "UHM",
        {
            "import": select_all,  # imports and exports
            "season": select_all,  # seasonally adjusted and not
            "country": _world_or_first,
            "unit": match("dkk", fallback_first=True),
            "items": match("total", fallback_first=True),
        },
    )
    flow_txt = df[cols["import"]].astype(str).str.lower()
    df["flow"] = flow_txt.map(lambda t: "exp" if "export" in t else ("imp" if "import" in t else None))
    seas_txt = df[cols["season"]].astype(str).str.lower()
    df["season"] = seas_txt.map(lambda t: "nsa" if any(k in t for k in ("not ", "non", "unadjusted")) else "sa")
    df = df.dropna(subset=["flow"])
    piv = df.pivot_table(index="date", columns=["season", "flow"], values="value", aggfunc="first")
    try:
        sa = (piv[("sa", "exp")] - piv[("sa", "imp")]) / 1000  # m DKK -> DKK bn
        nsa = (piv[("nsa", "exp")] - piv[("nsa", "imp")]) / 1000
    except KeyError as e:
        raise DataError(f"UHM: could not build balance; found columns {list(piv.columns)}") from e
    out = pd.DataFrame({"date": sa.index, "value": sa.values, "value_nsa": nsa.reindex(sa.index).values})
    return Series(_clean(out), "Statistics Denmark (UHM)", "monthly",
                  "Exports minus imports of goods, DKK bn (customs-based external trade statistics).")


# --------------------------------------------------------------------------- #
# Registry used by the app and the validation script
# --------------------------------------------------------------------------- #
METRICS = [
    dict(key="gdp", title="Real GDP growth", unit="% y/y", loader=load_gdp_growth, ttl=6 * 3600),
    dict(key="core", title="Core inflation", unit="% y/y", loader=load_core_inflation, ttl=6 * 3600),
    dict(key="yield", title="10-year government bond yield", unit="%", loader=load_bond_yield, ttl=3600),
    dict(key="debt", title="Gross general government debt", unit="% of GDP", loader=load_gov_debt, ttl=12 * 3600),
    dict(key="fx", title="Nominal exchange rate, USD per DKK", unit="USD per DKK", loader=load_fx_nominal, ttl=3600),
    dict(key="rer", title="Real exchange rate, USD per DKK", unit="Index, 1996-2025 avg = 100", loader=load_fx_real, ttl=6 * 3600),
    dict(key="trade", title="Goods trade balance", unit="DKK bn", loader=load_trade_balance, ttl=6 * 3600),
]
