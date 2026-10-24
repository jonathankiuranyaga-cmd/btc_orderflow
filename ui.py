"""Presentation layer for the paper desk.

Streamlit's default widgets are functional but visually flat, and a trading
dashboard has to make several readings compete for attention at once. This
module holds the design tokens, the stylesheet, and the small HTML components
the app renders, so the look lives in one place instead of being scattered
through the page code.

Everything here is presentation only. No trading logic, no state, no data
fetching.
"""

from __future__ import annotations

from html import escape

# Design tokens. These mirror the values in .streamlit/config.toml so the
# custom components match the native widgets instead of fighting them.
INK = "#0f172a"
INK_SOFT = "#475569"
INK_FAINT = "#64748b"
SURFACE = "#ffffff"
SURFACE_ALT = "#f8fafc"
SURFACE_ELEVATED = "#ffffff"
BORDER = "#e2e8f0"
BORDER_STRONG = "#cbd5e1"
LONG = "#059669"
LONG_SOFT = "#ecfdf5"
SHORT = "#dc2626"
SHORT_SOFT = "#fef2f2"
NEUTRAL = "#475569"
NEUTRAL_SOFT = "#f1f5f9"
ACCENT = "#2563eb"
ACCENT_SOFT = "#eff6ff"
WARN = "#d97706"
WARN_SOFT = "#fffbeb"
INFO = "#0891b2"
INFO_SOFT = "#ecfeff"


def inject_styles() -> None:
    """Install the stylesheet. Called once when the app starts."""
    import streamlit as st

    st.markdown(
        f"""
        <style>
        /* ---- layout --------------------------------------------------- */
        .block-container {{
            padding-top: 1.8rem;
            padding-bottom: 4rem;
            max-width: 1600px;
        }}
        #MainMenu, footer, header [data-testid="stStatusWidget"] {{ visibility: hidden; }}

        /* ---- typography ----------------------------------------------- */
        h1, h2, h3, h4 {{
            letter-spacing: -0.02em;
            font-weight: 700;
            color: {INK};
        }}
        h2 {{
            font-size: 0.95rem !important;
            text-transform: uppercase;
            letter-spacing: 0.12em;
            color: {INK_SOFT};
            margin: 1.8rem 0 0.6rem 0 !important;
            padding-bottom: 0.5rem;
            border-bottom: 1px solid {BORDER};
        }}
        h3 {{ font-size: 0.98rem !important; color: {INK}; font-weight: 650; }}
        p, span, label, div {{ color: {INK}; }}
        .stMarkdown p {{ line-height: 1.7; }}

        /* ---- metric cards --------------------------------------------- */
        div[data-testid="stMetric"] {{
            background: linear-gradient(180deg, {SURFACE_ELEVATED} 0%, {SURFACE_ALT} 100%);
            border: 1px solid {BORDER};
            border-radius: 14px;
            padding: 0.8rem 1rem 0.9rem 1rem;
            box-shadow: 0 1px 1px rgba(15, 23, 42, 0.02), 0 8px 30px -12px rgba(15, 23, 42, 0.4);
            transition: all 160ms ease;
        }}
        div[data-testid="stMetric"]:hover {{
            border-color: {BORDER_STRONG};
            box-shadow: 0 1px 1px rgba(15, 23, 42, 0.02), 0 12px 40px -12px rgba(37, 99, 235, 0.25);
            transform: translateY(-1px);
        }}
        div[data-testid="stMetricLabel"] p {{
            font-size: 0.7rem !important;
            font-weight: 650;
            letter-spacing: 0.09em;
            text-transform: uppercase;
            color: {INK_FAINT} !important;
        }}
        div[data-testid="stMetricValue"] {{
            font-size: 1.38rem !important;
            font-weight: 700;
            letter-spacing: -0.03em;
            font-variant-numeric: tabular-nums;
            color: {INK};
        }}
        div[data-testid="stMetricDelta"] {{
            font-size: 0.72rem !important;
            font-weight: 580;
        }}
        div[data-testid="stMetricDelta"] svg {{ display: none; }}

        /* ---- tabs as a segmented control ------------------------------- */
        button[data-baseweb="tab"] {{
            font-size: 0.8rem;
            font-weight: 620;
            letter-spacing: 0.01em;
            padding: 0.55rem 0.25rem;
            color: {INK_SOFT};
        }}
        button[data-baseweb="tab"] > div {{ font-size: 0.8rem !important; }}
        button[data-baseweb="tab"][aria-selected="true"] {{
            color: {ACCENT};
            font-weight: 700;
        }}
        div[data-baseweb="tab-list"] {{
            position: sticky;
            top: 0.4rem;
            z-index: 60;
            background: rgba(255, 255, 255, 0.88);
            backdrop-filter: blur(10px);
            border: 1px solid {BORDER};
            border-radius: 999px;
            padding: 0.25rem 0.5rem;
            gap: 0.2rem;
            margin-bottom: 0.8rem;
            box-shadow: 0 1px 1px rgba(15, 23, 42, 0.02), 0 10px 30px -18px rgba(15, 23, 42, 0.5);
            flex-wrap: nowrap;
            overflow-x: auto;
        }}
        div[data-baseweb="tab-list"]::-webkit-scrollbar {{ height: 0; }}
        button[data-baseweb="tab"] {{
            border-radius: 999px;
            padding: 0.45rem 0.9rem !important;
            transition: all 140ms ease;
            white-space: nowrap;
        }}
        button[data-baseweb="tab"]:hover {{ background: {ACCENT_SOFT}; color: {ACCENT}; }}
        div[data-baseweb="tab-highlight"] {{
            background-color: {ACCENT};
            border-radius: 999px;
            height: 3px;
            box-shadow: 0 -4px 12px -2px rgba(37, 99, 235, 0.5);
        }}
        div[data-baseweb="tab-border"] {{ background-color: transparent; }}

        /* ---- containers ------------------------------------------------ */
        div[data-testid="stVerticalBlockBorderWrapper"] > div {{
            border-radius: 16px;
            border-color: {BORDER};
            background: linear-gradient(180deg, {SURFACE_ELEVATED} 0%, {SURFACE_ALT} 100%);
            box-shadow: 0 1px 1px rgba(15, 23, 42, 0.02), 0 20px 60px -30px rgba(15, 23, 42, 0.6);
        }}

        /* ---- buttons --------------------------------------------------- */
        button[kind="primary"] {{
            border-radius: 999px;
            font-weight: 650;
            letter-spacing: 0.01em;
            box-shadow: 0 1px 1px rgba(15, 23, 42, 0.08), 0 10px 40px -12px rgba(37, 99, 235, 0.9);
            padding: 0.55rem 1.2rem;
        }}
        button[kind="secondary"], button[kind="tertiary"] {{
            border-radius: 999px;
            border-color: {BORDER_STRONG};
            color: {INK};
            font-weight: 600;
            padding: 0.55rem 1.1rem;
        }}
        button[kind="primary"]:disabled, button[kind="secondary"]:disabled {{
            opacity: 0.38;
        }}

        /* ---- dataframes and expander ----------------------------------- */
        div[data-testid="stDataFrame"] {{
            border: 1px solid {BORDER};
            border-radius: 16px;
            overflow: hidden;
            box-shadow: 0 1px 1px rgba(15, 23, 42, 0.02);
        }}
        div[data-testid="stExpander"] {{
            border: 1px solid {BORDER};
            border-radius: 16px;
            background: linear-gradient(180deg, {SURFACE_ELEVATED} 0%, {SURFACE_ALT} 100%);
            box-shadow: 0 1px 1px rgba(15, 23, 42, 0.02);
        }}
        details summary {{
            font-weight: 650;
            color: {INK};
        }}

        /* ---- sidebar --------------------------------------------------- */
        section[data-testid="stSidebar"] {{
            border-right: 1px solid rgba(148, 163, 184, 0.25);
            background: radial-gradient(1200px 800px at 0% 0%, #17233b 0%, #101a2b 55%, #0b1324 100%);
        }}
        section[data-testid="stSidebar"] .stMarkdown p,
        section[data-testid="stSidebar"] .stMarkdown span,
        section[data-testid="stSidebar"] label p,
        section[data-testid="stSidebar"] .stNumberInput label p,
        section[data-testid="stSidebar"] .stSlider label p,
        section[data-testid="stSidebar"] .stCheckbox label p {{
            color: #dbe6ff !important;
        }}
        section[data-testid="stSidebar"] h2 {{
            color: #f8fafc !important;
            border-bottom: 1px solid rgba(148, 163, 184, 0.35);
        }}
        section[data-testid="stSidebar"] hr {{
            border-color: rgba(148, 163, 184, 0.35);
            margin-top: 1.4rem !important;
        }}
        section[data-testid="stSidebar"] input, section[data-testid="stSidebar"] [data-baseweb="slider"] {{
            color-scheme: dark;
        }}

        /* ---- inputs ---------------------------------------------------- */
        div[data-baseweb="select"] > div,
        div[data-baseweb="input"] > div,
        div[data-baseweb="textarea"] > div {{
            border-radius: 999px;
            border-color: {BORDER};
            background: {SURFACE};
        }}
        section[data-testid="stSidebar"] div[data-baseweb="select"] > div,
        section[data-testid="stSidebar"] div[data-baseweb="input"] > div {{
            background: rgba(15, 23, 42, 0.55);
            border-color: rgba(148, 163, 184, 0.45);
        }}

        /* ---- scrollbars ------------------------------------------------ */
        ::-webkit-scrollbar {{ width: 9px; height: 9px; }}
        ::-webkit-scrollbar-track {{ background: transparent; }}
        ::-webkit-scrollbar-thumb {{
            background: {BORDER_STRONG};
            border-radius: 6px;
        }}
        ::-webkit-scrollbar-thumb:hover {{ background: {INK_FAINT}; }}

        /* ---- subtle entrance for freshly rendered metric cards ---------- */
        div[data-testid="stMetric"] {{ animation: of-rise 260ms ease both; }}
        @keyframes of-rise {{
            from {{ opacity: 0; transform: translateY(4px); }}
            to   {{ opacity: 1; transform: translateY(0); }}
        }}

        /* ---- custom components ----------------------------------------- */
        .of-header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 1rem;
            margin: 0 0 0.9rem 0;
            flex-wrap: wrap;
        }}
        .of-brand {{ display: flex; align-items: center; gap: 0.85rem; }}
        .of-logo {{
            width: 46px; height: 46px;
            display: flex; align-items: center; justify-content: center;
            font-size: 1.5rem; font-weight: 800; color: #f8fafc;
            background: linear-gradient(135deg, {ACCENT} 0%, #7c3aed 100%);
            border-radius: 14px;
            box-shadow: 0 8px 24px -8px rgba(37, 99, 235, 0.55);
            flex: 0 0 auto;
        }}
        .of-header h1 {{
            font-size: 1.6rem;
            font-weight: 800;
            letter-spacing: -0.05em;
            margin: 0;
            color: {INK};
        }}
        .of-header .of-sub {{
            font-size: 0.74rem;
            color: {INK_FAINT};
            letter-spacing: 0.14em;
            text-transform: uppercase;
            font-weight: 650;
            margin-top: 0.15rem;
        }}
        .of-rule {{
            height: 1px;
            background: radial-gradient(600px 1px at 50% 0%, {BORDER_STRONG} 0%, rgba(203, 213, 225, 0) 70%);
            margin: 0 0 1.2rem 0;
        }}

        .of-card {{
            background: radial-gradient(800px 400px at 50% 0%, {SURFACE_ELEVATED} 0%, {SURFACE_ALT} 100%);
            border: 1px solid {BORDER};
            border-radius: 18px;
            padding: 1.1rem 1.4rem 1.2rem 1.4rem;
            box-shadow: 0 1px 1px rgba(15, 23, 42, 0.02), 0 40px 80px -60px rgba(15, 23, 42, 0.8);
            margin-bottom: 1rem;
        }}
        .of-card.accent-long {{ border-top: 2px solid {LONG}; }}
        .of-card.accent-short {{ border-top: 2px solid {SHORT}; }}
        .of-card.accent-wait {{ border-top: 2px solid {BORDER_STRONG}; }}
        .of-card.accent-warn {{ border-top: 2px solid {WARN}; }}

        .of-banner {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 1.4rem;
            flex-wrap: wrap;
        }}
        .of-banner-main {{ min-width: 240px; flex: 1 1 320px; }}
        .of-label {{
            font-size: 0.66rem;
            font-weight: 700;
            letter-spacing: 0.13em;
            text-transform: uppercase;
            color: {INK_FAINT};
            margin-bottom: 0.28rem;
        }}
        .of-signal {{
            font-size: 2.4rem;
            font-weight: 800;
            letter-spacing: -0.06em;
            line-height: 1.0;
        }}
        .of-signal.long {{ color: {LONG}; }}
        .of-signal.short {{ color: {SHORT}; }}
        .of-signal.wait {{ color: {INK_SOFT}; }}
        .of-subtext {{
            font-size: 0.8rem;
            color: {INK_SOFT};
            margin-top: 0.3rem;
        }}
        .of-pill {{
            display: inline-block;
            padding: 0.22rem 0.7rem;
            border-radius: 999px;
            font-size: 0.7rem;
            font-weight: 700;
            letter-spacing: 0.09em;
            text-transform: uppercase;
            border: 1px solid transparent;
        }}
        .of-pill.long {{ background: {LONG_SOFT}; color: {LONG}; border-color: rgba(5, 150, 105, 0.25); }}
        .of-pill.short {{ background: {SHORT_SOFT}; color: {SHORT}; border-color: rgba(220, 38, 38, 0.25); }}
        .of-pill.neutral {{ background: {NEUTRAL_SOFT}; color: {NEUTRAL}; border-color: {BORDER}; }}
        .of-pill.warn {{ background: {WARN_SOFT}; color: {WARN}; border-color: rgba(217, 119, 6, 0.25); }}

        .of-levels {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(112px, 1fr));
            gap: 0.55rem;
            margin-top: 0.85rem;
        }}
        .of-level {{
            background: linear-gradient(180deg, {SURFACE_ELEVATED} 0%, rgba(248, 250, 252, 0.9) 100%);
            border: 1px solid {BORDER};
            border-radius: 12px;
            padding: 0.6rem 0.8rem;
        }}
        .of-level .of-label {{ margin-bottom: 0.2rem; font-size: 0.62rem; }}
        .of-level .of-value {{
            font-size: 1.08rem;
            font-weight: 680;
            font-variant-numeric: tabular-nums;
            letter-spacing: -0.02em;
        }}
        .of-level .of-value.long {{ color: {LONG}; }}
        .of-level .of-value.short {{ color: {SHORT}; }}

        .of-gauge {{ min-width: 210px; flex: 0 1 240px; }}
        .of-gauge-head {{
            display: flex;
            align-items: baseline;
            justify-content: space-between;
            margin-bottom: 0.35rem;
        }}
        .of-gauge-score {{
            font-size: 1.5rem;
            font-weight: 700;
            font-variant-numeric: tabular-nums;
            letter-spacing: -0.02em;
        }}
        .of-gauge-track {{
            height: 10px;
            border-radius: 999px;
            background: radial-gradient(circle, {BORDER} 0%, rgba(226, 232, 240, 0.6) 100%);
            overflow: hidden;
        }}
        .of-gauge-fill {{
            height: 100%; border-radius: 999px;
            background-image: linear-gradient(90deg, rgba(255,255,255,0.28) 0%, rgba(255,255,255,0) 55%);
            transition: width 400ms ease;
        }}
        .of-gauge-scale {{
            display: flex;
            justify-content: space-between;
            font-size: 0.62rem;
            color: {INK_FAINT};
            margin-top: 0.28rem;
            letter-spacing: 0.04em;
        }}

        .of-reason {{
            font-size: 0.83rem;
            color: {INK_SOFT};
            margin-top: 0.85rem;
            padding-top: 0.7rem;
            border-top: 1px solid {BORDER};
            line-height: 1.5;
        }}

        .of-strip {{
            display: flex;
            flex-wrap: wrap;
            gap: 0.4rem;
            margin-bottom: 0.6rem;
        }}
        .of-chip {{
            display: inline-flex;
            align-items: center;
            gap: 0.4rem;
            font-size: 0.72rem;
            font-weight: 580;
            color: {INK_SOFT};
            background: linear-gradient(180deg, {SURFACE_ELEVATED} 0%, {SURFACE_ALT} 100%);
            border: 1px solid {BORDER};
            border-radius: 999px;
            padding: 0.26rem 0.7rem;
            box-shadow: 0 1px 1px rgba(15, 23, 42, 0.01);
        }}
        .of-dot {{
            width: 7px; height: 7px; border-radius: 50%;
            display: inline-block;
        }}
        .of-dot.ok {{ background: {LONG}; box-shadow: 0 0 6px rgba(5, 150, 105, 0.6); }}
        .of-dot.bad {{ background: {SHORT}; box-shadow: 0 0 6px rgba(220, 38, 38, 0.6); }}
        .of-dot.warn {{ background: {WARN}; box-shadow: 0 0 6px rgba(217, 119, 6, 0.6); }}
        .of-dot.idle {{ background: {INK_FAINT}; }}

        .of-spark {{
            font-variant-numeric: tabular-nums;
            font-size: 0.78rem;
            color: {INK_SOFT};
        }}
        .of-note {{
            font-size: 0.76rem;
            color: {INK_FAINT};
            line-height: 1.55;
            margin-top: 0.5rem;
        }}
        .of-kv {{ width: 100%; border-collapse: collapse; font-size: 0.8rem; }}
        .of-kv td {{ padding: 0.3rem 0.5rem; border-bottom: 1px solid {BORDER}; }}
        .of-kv td:last-child {{
            text-align: right;
            font-weight: 620;
            font-variant-numeric: tabular-nums;
        }}
        .of-kv tr:last-child td {{ border-bottom: none; }}

        .of-alert {{
            border-radius: 14px;
            padding: 0.8rem 1rem;
            font-size: 0.84rem;
            font-weight: 560;
            line-height: 1.6;
            border: 1px solid;
            box-shadow: 0 1px 1px rgba(15, 23, 42, 0.01);
        }}
        .of-alert.warn {{ background: linear-gradient(180deg, {WARN_SOFT} 0%, rgba(255, 251, 235, 0.8) 100%); border-color: rgba(217, 119, 6, 0.4); color: #92400e; }}
        .of-alert.bad {{ background: linear-gradient(180deg, {SHORT_SOFT} 0%, rgba(254, 242, 242, 0.8) 100%); border-color: rgba(220, 38, 38, 0.4); color: #991b1b; }}
        .of-alert.ok {{ background: linear-gradient(180deg, {LONG_SOFT} 0%, rgba(236, 253, 245, 0.8) 100%); border-color: rgba(5, 150, 105, 0.4); color: #065f46; }}
        .of-alert.info {{ background: linear-gradient(180deg, {ACCENT_SOFT} 0%, rgba(239, 246, 255, 0.8) 100%); border-color: rgba(37, 99, 235, 0.35); color: #1e40af; }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def header(title: str, subtitle: str) -> None:
    """Render the page masthead."""
    import streamlit as st

    st.markdown(
        f"""
        <div class="of-header">
            <div class="of-brand">
              <div class="of-logo">₿</div>
              <div>
                <h1>{escape(title)}</h1>
                <div class="of-sub">{escape(subtitle)}</div>
              </div>
            </div>
            <span class="of-pill warn" title="Simulated fills only — no real orders are ever sent">Paper execution only</span>
        </div>
        <div class="of-rule"></div>
        """,
        unsafe_allow_html=True,
    )


def signal_class(signal: str) -> str:
    if signal == "LONG":
        return "long"
    if signal == "SHORT":
        return "short"
    return "wait"


def gauge_color(score: int) -> str:
    if score >= 75:
        return LONG
    if score >= 55:
        return ACCENT
    if score >= 35:
        return WARN
    return INK_FAINT


def decision_banner(
    *,
    signal: str,
    confidence: str,
    conviction_score: int,
    conviction_band: str,
    state: str,
    entry: float | None,
    stop: float | None,
    target: float | None,
    reason: str,
    reward_risk: str,
    breakeven: str,
) -> None:
    """Render the primary decision card."""
    import streamlit as st

    tone = signal_class(signal)
    accent = {
        "long": "accent-long",
        "short": "accent-short",
        "wait": "accent-wait",
    }[tone]
    pill_tone = tone if signal in ("LONG", "SHORT") else "neutral"
    levels = []
    for label, value, value_tone in (
        ("Entry", entry, ""),
        ("Stop", stop, "short" if tone == "long" else ("long" if tone == "short" else "")),
        ("Target", target, "long" if tone == "long" else ("short" if tone == "short" else "")),
    ):
        shown = f"{value:,.2f}" if value is not None else "—"
        levels.append(
            f'<div class="of-level"><div class="of-label">{label}</div>'
            f'<div class="of-value {value_tone}">{shown}</div></div>'
        )
    extra = []
    extra.append(
        f'<div class="of-level"><div class="of-label">Reward / risk</div>'
        f'<div class="of-value">{escape(reward_risk)}</div></div>'
    )
    extra.append(
        f'<div class="of-level"><div class="of-label">Breakeven win rate</div>'
        f'<div class="of-value">{escape(breakeven)}</div></div>'
    )
    fill = max(0, min(100, int(conviction_score)))
    color = gauge_color(fill)
    st.markdown(
        f"""
        <div class="of-card {accent}">
          <div class="of-banner">
            <div class="of-banner-main">
              <div class="of-label">Signal</div>
              <div class="of-signal {tone}">{escape(signal)}</div>
              <div class="of-subtext">
                <span class="of-pill {pill_tone}">{escape(confidence)}</span>
                <span class="of-pill neutral" style="margin-left:0.35rem">{escape(state)}</span>
              </div>
            </div>
            <div class="of-gauge">
              <div class="of-gauge-head">
                <div class="of-label" style="margin:0">Conviction</div>
                <div class="of-gauge-score" style="color:{color}">{fill}<span style="font-size:0.85rem;color:{INK_SOFT}">/100</span></div>
              </div>
              <div class="of-gauge-track">
                <div class="of-gauge-fill" style="width:{fill}%;background:{color}"></div>
              </div>
              <div class="of-gauge-scale"><span>0</span><span>{escape(conviction_band)}</span><span>100</span></div>
            </div>
          </div>
          <div class="of-levels">{"".join(levels)}{"".join(extra)}</div>
          <div class="of-reason">{escape(reason)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def alert(kind: str, message: str) -> None:
    """Render a styled callout."""
    import streamlit as st

    st.markdown(
        f'<div class="of-alert {kind}">{escape(message)}</div>',
        unsafe_allow_html=True,
    )


def chips(items: list[tuple[str, str]]) -> None:
    """Render a row of status chips.

    Each item is a ``(label, tone)`` pair where tone is one of
    ``ok``, ``bad``, ``warn`` or ``idle``.
    """
    import streamlit as st

    parts = []
    for label, tone in items:
        parts.append(
            f'<span class="of-chip"><span class="of-dot {tone}"></span>{escape(label)}</span>'
        )
    st.markdown(f'<div class="of-strip">{"".join(parts)}</div>', unsafe_allow_html=True)


def key_values(rows: list[tuple[str, str]]) -> None:
    """Render a compact two-column definition table."""
    import streamlit as st

    body = "".join(
        f"<tr><td>{escape(str(label))}</td><td>{escape(str(value))}</td></tr>" for label, value in rows
    )
    st.markdown(f'<table class="of-kv">{body}</table>', unsafe_allow_html=True)


def note(message: str) -> None:
    """Render small explanatory text."""
    import streamlit as st

    st.markdown(f'<div class="of-note">{escape(message)}</div>', unsafe_allow_html=True)


def section(label: str) -> None:
    """Render a small uppercase section label."""
    import streamlit as st

    st.markdown(
        f'<div class="of-label" style="margin:0.2rem 0 0.3rem 0">{escape(label)}</div>',
        unsafe_allow_html=True,
    )
