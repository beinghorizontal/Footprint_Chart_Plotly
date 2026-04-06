"""
footprint_plotly.py — Order-Book Footprint Chart (Plotly)
=========================================================
Adaptive-bucket footprint chart built from Level-2 order-book snapshots.

Each time bar is split into N equal price buckets (adaptive to that bar's
range), and each cell shows aggregated bid vs offer pressure.  Cell colour
intensity reflects total bid-ask volume.

Layout
------
  Row 1 : Footprint cells  +  OHLC candle body/wick overlay
  Row 2 : Per-bar Delta bar chart  (Σ bid − Σ offer)

Performance
-----------
Optimised for live / Dash use (~82 DOM nodes vs ~577 in a naïve
shape-per-cell approach — 85 % reduction):

  • Cell backgrounds  → 2 Bar traces  (bid + offer)
  • Cell text labels   → 1 Scatter(mode='text') with per-point colours
  • Candle overlays    → ~50 lightweight shapes  (unchanged)

Usage
-----
    python footprint_plotly.py --csv data.csv               # save HTML
    python footprint_plotly.py --csv data.csv --show         # open browser
    python footprint_plotly.py --csv data.csv --rows 5       # rows per bar
    python footprint_plotly.py --csv data.csv --bars 15      # bar interval (min)
    python footprint_plotly.py --csv data.csv --title "My Chart"

CSV Format  (headerless, 6 columns)
------------------------------------
    bid_qty, bid_orders, offer_qty, offer_orders, datetime, price
"""

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
N_ROWS = 5  # price rows per bar
BAR_MIN = 15  # bar interval in minutes
MIN_RANGE = 2.0  # minimum bar range (pts) — avoids doji edge-case

# Theme (GitHub-dark inspired)
C_BG = "#0d1117"
C_TEXT = "#e6edf3"
C_GRID = "#21262d"
C_CELL_BDR = "#30363d"
C_CANDLE_UP = "#3fb950"
C_CANDLE_DOWN = "#f85149"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def fmt_k(v: float) -> str:
    """Human-readable thousands / millions."""
    if v >= 1_000_000:
        return f"{v / 1e6:.1f}M"
    if v >= 1_000:
        return f"{v / 1e3:.0f}K"
    return f"{v:.0f}"


def rgba(hex_color: str, alpha: float) -> str:
    """Hex colour → rgba() string."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha:.2f})"


def contrast_text_color(hex_bg: str, alpha: float) -> str:
    """Return black or white depending on effective cell luminance."""
    h = hex_bg.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    # Blend foreground over plot background (#0a0e14)
    bg_r, bg_g, bg_b = 10, 14, 20
    eff_r = r * alpha + bg_r * (1 - alpha)
    eff_g = g * alpha + bg_g * (1 - alpha)
    eff_b = b * alpha + bg_b * (1 - alpha)
    lum = 0.2126 * eff_r + 0.7152 * eff_g + 0.0722 * eff_b
    return "#000000" if lum > 140 else "#ffffff"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_data(csv_path: str) -> pd.DataFrame:
    """
    Read a headerless CSV with columns:
        bid_qty, bid_orders, offer_qty, offer_orders, datetime, price

    Returns a DataFrame with derived ``avg_bid`` and ``avg_offer`` columns
    (qty / orders per row).
    """
    df = pd.read_csv(
        csv_path,
        header=None,
        names=["bid_qty", "bid_orders", "offer_qty", "offer_orders", "datetime", "price"],
    )
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.sort_values("datetime").reset_index(drop=True)

    df["avg_bid"] = df["bid_qty"] / df["bid_orders"].replace(0, np.nan)
    df["avg_offer"] = df["offer_qty"] / df["offer_orders"].replace(0, np.nan)
    df["avg_bid"] = df["avg_bid"].fillna(0)
    df["avg_offer"] = df["avg_offer"].fillna(0)

    return df


# ---------------------------------------------------------------------------
# Adaptive-bucket aggregation
# ---------------------------------------------------------------------------
def build_footprint(df: pd.DataFrame, n_rows: int = N_ROWS, bar_min: int = BAR_MIN):
    """
    For each time bar:
      1. Compute High / Low from snapshots in that bar.
      2. Divide [Low, High] into *n_rows* equal buckets.
      3. Assign each snapshot to a bucket by price.
      4. Sum avg_bid and avg_offer per bucket.

    Returns
    -------
    list[dict]
        Each dict contains:
        ``bar``, ``open``, ``high``, ``low``, ``close``,
        ``cells`` (list of row dicts), ``bar_delta``, ``bar_total``.
    """
    freq = f"{bar_min}min"
    df["bar"] = df["datetime"].dt.floor(freq)

    bars_out = []

    for bar_ts, grp in df.groupby("bar", sort=True):
        prices = grp["price"]
        hi = prices.max()
        lo = prices.min()
        op = prices.iloc[0]
        cl = prices.iloc[-1]

        if (hi - lo) < MIN_RANGE:
            mid = (hi + lo) / 2
            hi = mid + MIN_RANGE / 2
            lo = mid - MIN_RANGE / 2

        bucket_size = (hi - lo) / n_rows
        edges = [lo + i * bucket_size for i in range(n_rows + 1)]

        bucket_idx = pd.cut(
            grp["price"],
            bins=edges,
            labels=list(range(n_rows)),
            include_lowest=True,
        ).astype(float)

        grp = grp.copy()
        grp["bucket"] = bucket_idx

        agg = (
            grp.groupby("bucket")
            .agg(bid=("avg_bid", "sum"), offer=("avg_offer", "sum"))
            .reindex(range(n_rows), fill_value=0)
        )

        cells = []
        for i in range(n_rows):
            bv = agg.loc[i, "bid"]
            ov = agg.loc[i, "offer"]
            cells.append(
                {
                    "y_lo": edges[i],
                    "y_hi": edges[i + 1],
                    "y_mid": (edges[i] + edges[i + 1]) / 2,
                    "bid": bv,
                    "offer": ov,
                    "delta": bv - ov,
                }
            )

        total_bid = agg["bid"].sum()
        total_offer = agg["offer"].sum()

        bars_out.append(
            {
                "bar": bar_ts,
                "open": op,
                "high": hi,
                "low": lo,
                "close": cl,
                "cells": cells,
                "bar_delta": total_bid - total_offer,
                "bar_total": total_bid + total_offer,
            }
        )

    return bars_out


# ---------------------------------------------------------------------------
# Figure builder  (optimised for live / Dash use)
# ---------------------------------------------------------------------------
def build_figure(bars_data: list, n_rows: int = N_ROWS, title: str = "") -> go.Figure:
    """Build the two-row Plotly figure from pre-computed bar data."""

    if not title:
        first = bars_data[0]["bar"].strftime("%d %b %Y")
        title = f"Footprint Chart  |  {first}"

    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.06,
        subplot_titles=(
            f"Footprint  ({n_rows} rows/bar)  |  Bid (green)  Offer (red)  +  OHLC",
            "Bar Delta  (Avg Bid − Avg Offer)",
        ),
        row_heights=[0.78, 0.22],
    )

    n_bars = len(bars_data)
    tickvals = list(range(n_bars))
    ticktext = [b["bar"].strftime("%H:%M") for b in bars_data]

    # Global max volume (for colour scaling)
    all_cells = [c for b in bars_data for c in b["cells"]]
    max_vol = max((c["bid"] + c["offer"] for c in all_cells), default=1.0)

    # ------------------------------------------------------------------
    # Cell backgrounds  →  2 Bar traces  (bid left-half, offer right-half)
    # ------------------------------------------------------------------
    bid_x, bid_base, bid_h, bid_clr = [], [], [], []
    off_x, off_base, off_h, off_clr = [], [], [], []

    for xi, bar in enumerate(bars_data):
        for cell in bar["cells"]:
            bv, ov = cell["bid"], cell["offer"]
            alpha_b = 0.10 + 0.85 * min(bv / max(max_vol * 0.5, 1), 1.0)
            alpha_o = 0.10 + 0.85 * min(ov / max(max_vol * 0.5, 1), 1.0)

            h = cell["y_hi"] - cell["y_lo"]

            bid_x.append(xi - 0.23)
            bid_base.append(cell["y_lo"])
            bid_h.append(h)
            bid_clr.append(rgba(C_CANDLE_UP, alpha_b))

            off_x.append(xi + 0.23)
            off_base.append(cell["y_lo"])
            off_h.append(h)
            off_clr.append(rgba(C_CANDLE_DOWN, alpha_o))

    fig.add_trace(
        go.Bar(
            x=bid_x,
            y=bid_h,
            base=bid_base,
            marker=dict(color=bid_clr, line=dict(color=C_CELL_BDR, width=0.4)),
            width=0.46,
            showlegend=False,
            hoverinfo="skip",
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Bar(
            x=off_x,
            y=off_h,
            base=off_base,
            marker=dict(color=off_clr, line=dict(color=C_CELL_BDR, width=0.4)),
            width=0.46,
            showlegend=False,
            hoverinfo="skip",
        ),
        row=1,
        col=1,
    )

    # ------------------------------------------------------------------
    # Hover scatter  (invisible markers — rich tooltip on hover)
    # ------------------------------------------------------------------
    hov_x, hov_y, hov_txt = [], [], []

    for xi, bar in enumerate(bars_data):
        for cell in bar["cells"]:
            bv = cell["bid"]
            ov = cell["offer"]
            tvol = bv + ov
            y_mid = cell["y_mid"]

            ds = "+" if cell["delta"] >= 0 else ""
            hov_x.append(xi)
            hov_y.append(y_mid)
            hov_txt.append(
                f"<b>{bar['bar'].strftime('%H:%M')}</b>  @{y_mid:.1f}<br>"
                f"Bid: {fmt_k(bv)}  |  Offer: {fmt_k(ov)}<br>"
                f"Delta: {ds}{fmt_k(cell['delta'])}<br>"
                f"Total: {fmt_k(tvol)}"
            )

    fig.add_trace(
        go.Scatter(
            x=hov_x,
            y=hov_y,
            mode="markers",
            marker=dict(symbol="square", size=10, opacity=0),
            hovertemplate="%{text}<extra></extra>",
            text=hov_txt,
            showlegend=False,
        ),
        row=1,
        col=1,
    )

    # ------------------------------------------------------------------
    # Cell text  →  1 Scatter(mode='text') with per-point contrast colours
    # ------------------------------------------------------------------
    txt_x, txt_y, txt_labels, txt_colors = [], [], [], []

    for xi, bar in enumerate(bars_data):
        for cell in bar["cells"]:
            bv, ov = cell["bid"], cell["offer"]
            y_mid = cell["y_mid"]

            alpha_b = 0.10 + 0.85 * min(bv / max(max_vol * 0.5, 1), 1.0)
            alpha_o = 0.10 + 0.85 * min(ov / max(max_vol * 0.5, 1), 1.0)

            if bv > 0:
                txt_x.append(xi - 0.23)
                txt_y.append(y_mid)
                txt_labels.append(fmt_k(bv))
                txt_colors.append(contrast_text_color(C_CANDLE_UP, alpha_b))

            if ov > 0:
                txt_x.append(xi + 0.23)
                txt_y.append(y_mid)
                txt_labels.append(fmt_k(ov))
                txt_colors.append(contrast_text_color(C_CANDLE_DOWN, alpha_o))

    fig.add_trace(
        go.Scatter(
            x=txt_x,
            y=txt_y,
            mode="text",
            text=txt_labels,
            textfont=dict(color=txt_colors, size=8, family="'Courier New', monospace"),
            showlegend=False,
            hoverinfo="skip",
        ),
        row=1,
        col=1,
    )

    # ------------------------------------------------------------------
    # OHLC candles overlaid  (thin wick + body)
    # ------------------------------------------------------------------
    for xi, bar in enumerate(bars_data):
        bull = bar["close"] >= bar["open"]
        clr = C_CANDLE_UP if bull else C_CANDLE_DOWN
        blo = min(bar["open"], bar["close"])
        bhi = max(bar["open"], bar["close"])

        fig.add_shape(
            type="line",
            x0=xi,
            x1=xi,
            y0=bar["low"],
            y1=bar["high"],
            line=dict(color=clr, width=1.5),
            row=1,
            col=1,
        )
        fig.add_shape(
            type="rect",
            x0=xi - 0.05,
            x1=xi + 0.05,
            y0=blo,
            y1=bhi if bhi > blo else blo + 0.5,
            fillcolor=clr,
            line_width=0,
            row=1,
            col=1,
        )

    # ------------------------------------------------------------------
    # Delta / Total annotations below each bar
    # ------------------------------------------------------------------
    y_min = min(b["low"] for b in bars_data)
    y_max = max(b["high"] for b in bars_data)
    y_ann = y_min - (y_max - y_min) * 0.04

    for xi, bar in enumerate(bars_data):
        dlt = bar["bar_delta"]
        tot = bar["bar_total"]
        ds = "+" if dlt >= 0 else ""
        dclr = C_CANDLE_UP if dlt >= 0 else C_CANDLE_DOWN

        fig.add_annotation(
            x=xi,
            y=y_ann,
            text=(
                f"<b style='color:{dclr}'>{ds}{fmt_k(dlt)}</b>"
                f"<br><span style='color:{C_TEXT}'>{fmt_k(tot)}</span>"
            ),
            showarrow=False,
            font=dict(size=8, color=dclr),
            align="center",
            bgcolor="rgba(13,17,23,0.80)",
            bordercolor=C_GRID,
            borderwidth=0.5,
            row=1,
            col=1,
        )

    # ------------------------------------------------------------------
    # Row 2 — Delta bar chart
    # ------------------------------------------------------------------
    delta_vals = [b["bar_delta"] for b in bars_data]
    total_vals = [b["bar_total"] for b in bars_data]
    delta_colors = [C_CANDLE_UP if v >= 0 else C_CANDLE_DOWN for v in delta_vals]
    hover_delta = [
        f"<b>{b['bar'].strftime('%H:%M')}</b><br>"
        f"Delta: {'+' if v >= 0 else ''}{fmt_k(v)}<br>"
        f"Total: {fmt_k(t)}"
        for b, v, t in zip(bars_data, delta_vals, total_vals)
    ]

    fig.add_trace(
        go.Bar(
            x=tickvals,
            y=delta_vals,
            marker_color=delta_colors,
            hovertemplate="%{customdata}<extra></extra>",
            customdata=hover_delta,
            textposition="none",
            width=0.7,
            name="Delta",
            showlegend=False,
        ),
        row=2,
        col=1,
    )

    fig.add_hline(y=0, line=dict(color="#444c56", width=1, dash="dash"), row=2, col=1)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    axis_base = dict(
        zeroline=False,
        tickfont=dict(color=C_TEXT, size=9),
        linecolor=C_GRID,
    )

    fig.update_layout(
        title=dict(text=title, font=dict(color=C_TEXT, size=14), x=0.5, xanchor="center"),
        paper_bgcolor=C_BG,
        plot_bgcolor="#0a0e14",
        font=dict(color=C_TEXT, family="'Inter','Segoe UI',sans-serif"),
        height=1000,
        margin=dict(l=70, r=30, t=70, b=50),
        hovermode="closest",
        showlegend=False,
        barmode="overlay",
    )

    for row in (1, 2):
        fig.update_xaxes(
            **axis_base,
            showgrid=False,
            tickvals=tickvals,
            ticktext=ticktext if row == 2 else [""] * n_bars,
            showticklabels=(row == 2),
            range=[-0.6, n_bars - 0.4],
            row=row,
            col=1,
        )

    fig.update_yaxes(
        **axis_base,
        showgrid=True,
        gridcolor=C_GRID,
        gridwidth=0.4,
        title_text="Price",
        tickformat="d",
        range=[y_ann - (y_max - y_min) * 0.04, y_max + (y_max - y_min) * 0.02],
        row=1,
        col=1,
    )
    fig.update_yaxes(
        **axis_base,
        showgrid=False,
        title_text="Delta",
        row=2,
        col=1,
    )

    # Re-style only the two subplot-title annotations
    for i, ann in enumerate(fig.layout.annotations):
        if i < 2:
            ann.font = dict(color=C_TEXT, size=11)

    return fig


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Order-Book Footprint Chart (Plotly)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "CSV format (headerless, 6 columns):\n"
            "  bid_qty, bid_orders, offer_qty, offer_orders, datetime, price"
        ),
    )
    parser.add_argument("--csv", required=True, help="Path to the order-book CSV file")
    parser.add_argument(
        "--show", action="store_true", help="Open chart in default browser"
    )
    parser.add_argument(
        "--rows",
        type=int,
        default=N_ROWS,
        help=f"Price rows per bar (default {N_ROWS})",
    )
    parser.add_argument(
        "--bars",
        type=int,
        default=BAR_MIN,
        help=f"Bar interval in minutes (default {BAR_MIN})",
    )
    parser.add_argument("--title", type=str, default="", help="Custom chart title")
    args = parser.parse_args()

    out_path = Path(args.csv).with_name(
        Path(args.csv).stem + f"_footprint_{args.bars}m_{args.rows}rows.html"
    )

    print(f"Loading  → {args.csv}")
    df = load_data(args.csv)
    print(f"Rows: {len(df):,}  |  Snapshots: {df['datetime'].nunique():,}")

    bars_data = build_footprint(df, n_rows=args.rows, bar_min=args.bars)
    avg_bucket = np.mean([(b["high"] - b["low"]) / args.rows for b in bars_data])
    print(
        f"Bars: {len(bars_data)}  |  Rows/bar: {args.rows}  "
        f"|  Avg bucket: {avg_bucket:.1f} pts"
    )

    fig = build_figure(bars_data, n_rows=args.rows, title=args.title)
    fig.write_html(str(out_path), include_plotlyjs="cdn")
    print(f"Saved  → {out_path}")

    if args.show:
        fig.show()


if __name__ == "__main__":
    main()
