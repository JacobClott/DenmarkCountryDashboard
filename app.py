"""Denmark economy dashboard (Streamlit).

Run locally:  streamlit run app.py
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dkdata import sources as S

st.set_page_config(page_title="Denmark Economy Dashboard", layout="wide")

LINE_LIGHT, LINE_DARK = "#2a78d6", "#3987e5"
GRID = "rgba(128,128,128,0.22)"
REF = "rgba(128,128,128,0.7)"


# --------------------------------------------------------------------------- #
# Cached data access. Exceptions are not cached by Streamlit, so a failed source
# is simply retried on the next rerun.
# --------------------------------------------------------------------------- #
LOADERS = {m["key"]: m["loader"] for m in S.METRICS}


# One cached function per refresh tier; the metric key is an argument so every metric
# gets its own cache entry (closures created in a loop would share one cache key).
@st.cache_data(ttl=3600, show_spinner=False)
def _load_1h(key: str):
    return LOADERS[key]()


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _load_6h(key: str):
    return LOADERS[key]()


@st.cache_data(ttl=12 * 3600, show_spinner=False)
def _load_12h(key: str):
    return LOADERS[key]()


TIER = {3600: _load_1h, 6 * 3600: _load_6h, 12 * 3600: _load_12h}
CACHED = {m["key"]: (lambda k=m["key"], f=TIER[m["ttl"]]: f(k)) for m in S.METRICS}


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _refresh_stamp() -> str:
    return datetime.now().strftime("%d %b %Y, %H:%M")


def _is_dark() -> bool:
    try:
        return st.context.theme.type == "dark"
    except Exception:  # older Streamlit or no theme info
        return False


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def period_label(ts: pd.Timestamp, freq: str) -> str:
    if freq == "daily":
        return ts.strftime("%d %b %Y")
    if freq == "monthly":
        return ts.strftime("%b %Y")
    if freq == "quarterly":
        return f"Q{(ts.month - 1) // 3 + 1} {ts.year}"
    return str(ts.year)


def signed(x: float, d: int = 1, suffix: str = "") -> str:
    return "n/a" if pd.isna(x) else f"{x:+.{d}f}{suffix}"


# Per-metric display settings: number format, hover text builder, reference line.
def _hover_default(fmt, unit):
    return lambda df, lab: [f"{l}<br><b>{fmt.format(v)}</b> {unit}" for l, v in zip(lab, df["value"])]


def _hover_gdp(df, lab):
    return [f"{l}<br><b>{signed(v, 1, '%')}</b> y/y<br>{signed(q, 1, '%')} q/q"
            for l, v, q in zip(lab, df["value"], df["qoq"])]


def _hover_rer(df, lab):
    return [f"{l}<br><b>{v:.1f}</b> (avg = 100)<br>Nominal: {n:.4f} USD/DKK"
            for l, v, n in zip(lab, df["value"], df["nominal"])]


DISPLAY = {
    "gdp": dict(fmt="{:+.1f}%", hover=_hover_gdp, zero=True, yfmt=".1f"),
    "core": dict(fmt="{:.1f}%", hover=_hover_default("{:.1f}", "% y/y"), zero=True, yfmt=".1f"),
    "yield": dict(fmt="{:.2f}%", hover=_hover_default("{:.2f}", "%"), zero=False, yfmt=".1f"),
    "debt": dict(fmt="{:.1f}% of GDP", hover=_hover_default("{:.1f}", "% of GDP"), zero=False, yfmt=".0f"),
    "fx": dict(fmt="{:.4f} USD", hover=_hover_default("{:.4f}", "USD per DKK"), zero=False, yfmt=".2f"),
    "rer": dict(fmt="{:.1f}", hover=_hover_rer, zero=False, yfmt=".0f", ref=100),
    "trade": dict(fmt="{:+.1f} DKK bn", hover=_hover_default("{:+.1f}", "DKK bn"), zero=True, yfmt=".0f"),
}


def build_figure(df, freq, cfg, unit, dark):
    labels = [period_label(t, freq) for t in df["date"]]
    fig = go.Figure(
        go.Scatter(
            x=df["date"],
            y=df["value"],
            mode="lines",
            line=dict(color=LINE_DARK if dark else LINE_LIGHT, width=2),
            customdata=cfg["hover"](df, labels),
            hovertemplate="%{customdata}<extra></extra>",
        )
    )
    if cfg.get("zero"):
        fig.add_hline(y=0, line_width=1, line_color=REF)
    if cfg.get("ref") is not None:
        fig.add_hline(y=cfg["ref"], line_width=1, line_dash="dot", line_color=REF,
                      annotation_text="1996-2025 average", annotation_position="top left",
                      annotation_font_size=11)
    fig.update_layout(
        height=300,
        margin=dict(l=8, r=8, t=8, b=8),
        showlegend=False,
        hovermode="x",  # callout box for the nearest point along x
        hoverlabel=dict(align="left"),
        xaxis=dict(showgrid=False, showspikes=True, spikemode="across", spikesnap="cursor",
                   spikethickness=1, spikedash="solid", spikecolor=REF),
        yaxis=dict(title=dict(text=unit, font=dict(size=12)), gridcolor=GRID, tickformat=cfg["yfmt"],
                   zeroline=False),
    )
    return fig


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #
st.title("Denmark economy dashboard")

top = st.columns([1.2, 2.2, 2.6, 4])
with top[0]:
    if st.button("Refresh data", type="primary", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
with top[1]:
    span = st.radio("Time range", ["5Y", "10Y", "20Y", "Max"], index=1, horizontal=True)
with top[2]:
    trade_view = st.radio("Trade balance", ["Seasonally adjusted", "Unadjusted"], horizontal=True)
with top[3]:
    st.caption(f"Data last fetched: {_refresh_stamp()}. Daily series cache 1h; monthly/quarterly 6-12h.")

years = {"5Y": 5, "10Y": 10, "20Y": 20, "Max": None}[span]

cols = st.columns(2, gap="large")
for i, m in enumerate(S.METRICS):
    cfg = DISPLAY[m["key"]]
    with cols[i % 2]:
        st.markdown(f"**{m['title']}**")
        try:
            series = CACHED[m["key"]]()
        except Exception as e:  # noqa: BLE001 - one broken source must not take down the page
            st.warning(f"Could not load this series right now. {str(e)[:400]}")
            continue

        df = series.df.copy()
        unit = m["unit"]
        if m["key"] == "trade" and trade_view == "Unadjusted":
            df["value"] = df["value_nsa"]
            df = df.dropna(subset=["value"])
        if years is not None:
            df = df[df["date"] >= df["date"].max() - pd.DateOffset(years=years)]

        df = df.reset_index(drop=True)
        last = df.dropna(subset=["value"]).iloc[-1]
        st.caption(
            f"Latest: **{cfg['fmt'].format(last['value'])}** ({period_label(last['date'], series.freq)}) "
            f"· {series.freq} · {series.source}"
        )
        st.plotly_chart(build_figure(df, series.freq, cfg, unit, _is_dark()),
                        use_container_width=True, config={"displaylogo": False})
        if series.note:
            st.caption(series.note)
        with st.expander("Data table"):
            show = df[["date", "value"]].copy()
            show["date"] = [period_label(t, series.freq) for t in show["date"]]
            st.dataframe(show.iloc[::-1].rename(columns={"value": unit}), hide_index=True,
                         use_container_width=True, height=240)
