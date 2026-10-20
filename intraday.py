"""5分足チャートの並べ見ページ（docs/intraday/）

- 対象銘柄の5分足を Yahoo Finance から取得し、data/intraday/<コード>.csv に毎日追記して蓄積する
  （Yahoo は5分足を直近60日分しか返さないため、保存しておかないと古い日が消える）
- 銘柄ごとのページに、日足（約1年）と、1日1枚の5分足チャートを新しい順に並べる
- 5分足には日経平均（^N225）を薄い線で重ね、10:20〜10:40 に色を付ける
"""
import datetime as dt
import html
import os

import pandas as pd
import yfinance as yf

TICKERS = {
    "8035": "東京エレクトロン",
    "4062": "イビデン",
    "6098": "リクルート",
}
INDEX = "^N225"
DATA_DIR = "data/intraday"
OUT_DIR = os.path.join(os.environ.get("SCREENER_OUT", "docs"), "intraday")
BAND = ("10:20", "10:40")  # 色を付ける時間帯

# 1日の5分足の枠（昼休みは詰める）。前場 9:00〜11:25、後場 12:30〜15:25
SLOTS = [f"{h}:{m:02d}" for h in (9, 10, 11) for m in range(0, 60, 5) if (h, m) < (11, 30)] + \
        [f"{h}:{m:02d}" for h in (12, 13, 14, 15) for m in range(0, 60, 5) if (12, 30) <= (h, m) < (15, 30)]
SLOT_IX = {s: i for i, s in enumerate(SLOTS)}
N_MORNING = SLOTS.index("12:30")


# ---------- データ ----------
def _flat(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    return df.dropna(subset=["open", "high", "low", "close"])


def fetch_5m(sym):
    df = yf.download(sym, period="60d", interval="5m", progress=False, auto_adjust=False)
    if df is None or df.empty:
        return pd.DataFrame()
    df = _flat(df)
    idx = df.index
    if idx.tz is not None:
        idx = idx.tz_convert("Asia/Tokyo").tz_localize(None)
    df.index = idx
    df.index.name = "dt"
    return df


def fetch_daily(sym):
    df = yf.download(sym, period="2y", interval="1d", progress=False, auto_adjust=False)
    if df is None or df.empty:
        return pd.DataFrame()
    df = _flat(df)
    df.index = pd.to_datetime(df.index).tz_localize(None) if df.index.tz is not None else pd.to_datetime(df.index)
    return df


def merge_store(name, new):
    """保存済みの5分足に今回分を足して保存し、全体を返す"""
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"{name}.csv")
    if os.path.exists(path):
        old = pd.read_csv(path, parse_dates=["dt"], index_col="dt")
        df = pd.concat([old, new]) if not new.empty else old
    else:
        df = new
    if df.empty:
        return df
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.round(2).to_csv(path)
    return df


# ---------- 描画の部品 ----------
def nice_ticks(lo, hi, n=5):
    span = hi - lo
    if span <= 0:
        return [lo]
    raw = span / n
    mag = 10 ** len(str(int(raw))) / 10 if raw >= 1 else 1
    step = min((s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw), default=raw)
    t = (int(lo / step) + 1) * step
    out = []
    while t < hi:
        out.append(t)
        t += step
    return out


def fmt(v):
    return f"{v:,.0f}" if v >= 1000 else f"{v:,.1f}"


def pct(a, b):
    return (a / b - 1) * 100 if b else 0.0


def cls(v):
    return "up" if v > 0 else "dn" if v < 0 else "mut"


def intraday_svg(day, idx_day, prev_close):
    """1日分の5分足（＋日経平均の線）をSVGで返す"""
    W, H, VH = 760, 250, 46
    L, R, T = 8, 58, 10
    pw = W - L - R
    step = pw / len(SLOTS)
    xs = lambda i: L + (i + 0.5) * step

    bars = [(SLOT_IX[t.strftime("%-H:%M")], r) for t, r in day.iterrows() if t.strftime("%-H:%M") in SLOT_IX]
    if not bars:
        return ""
    o0 = bars[0][1].open
    # 日経平均は「当日の始値からの変化率」を銘柄の始値に掛けて同じ軸に載せる
    nk = []
    if idx_day is not None and not idx_day.empty:
        nk_open = idx_day.iloc[0].open
        for t, r in idx_day.iterrows():
            k = t.strftime("%-H:%M")
            if k in SLOT_IX:
                nk.append((SLOT_IX[k], o0 * r.close / nk_open))
    vals = [r.high for _, r in bars] + [r.low for _, r in bars] + [v for _, v in nk]
    if prev_close:
        vals.append(prev_close)
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.06 or hi * 0.005
    lo, hi = lo - pad, hi + pad
    y = lambda v: T + (hi - v) / (hi - lo) * (H - T)
    vmax = max(r.volume for _, r in bars) or 1

    g = []
    # 10:20〜10:40 の帯
    b0, b1 = SLOT_IX[BAND[0]], SLOT_IX[BAND[1]]
    g.append(f'<rect x="{L + b0 * step:.1f}" y="{T}" width="{(b1 - b0) * step:.1f}" height="{H - T + VH + 6}" class="band"/>')
    # 目盛り
    for tv in nice_ticks(lo, hi):
        yy = y(tv)
        g.append(f'<line x1="{L}" x2="{L + pw}" y1="{yy:.1f}" y2="{yy:.1f}" class="grid"/>'
                 f'<text x="{L + pw + 4}" y="{yy + 4:.1f}" class="ax">{fmt(tv)}</text>')
    # 昼休みの区切り
    xm = L + N_MORNING * step
    g.append(f'<line x1="{xm:.1f}" x2="{xm:.1f}" y1="{T}" y2="{H + VH + 6}" class="lunch"/>')
    # 前日終値
    if prev_close:
        yy = y(prev_close)
        g.append(f'<line x1="{L}" x2="{L + pw}" y1="{yy:.1f}" y2="{yy:.1f}" class="prev"/>')
    # 日経平均
    if len(nk) > 1:
        pts = " ".join(f"{xs(i):.1f},{y(v):.1f}" for i, v in nk)
        g.append(f'<polyline points="{pts}" class="nk"/>')
    # ローソク足と出来高
    bw = max(step * 0.62, 1.5)
    for i, r in bars:
        c = "cu" if r.close >= r.open else "cd"
        x = xs(i)
        g.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{y(r.high):.1f}" y2="{y(r.low):.1f}" class="{c}w"/>')
        top, bot = y(max(r.open, r.close)), y(min(r.open, r.close))
        g.append(f'<rect x="{x - bw / 2:.1f}" y="{top:.1f}" width="{bw:.1f}" height="{max(bot - top, 0.8):.1f}" class="{c}"/>')
        vh = r.volume / vmax * VH
        g.append(f'<rect x="{x - bw / 2:.1f}" y="{H + 6 + VH - vh:.1f}" width="{bw:.1f}" height="{vh:.1f}" class="{c} vol"/>')
    # 前場の安値に印
    morning = [(i, r) for i, r in bars if i < N_MORNING]
    if morning:
        i, r = min(morning, key=lambda t: t[1].low)
        g.append(f'<circle cx="{xs(i):.1f}" cy="{y(r.low) + 7:.1f}" r="2.6" class="lowmk"/>')
    # 時刻
    for lab in ("9:00", "10:00", "11:00", "12:30", "13:30", "14:30", "15:25"):
        i = SLOT_IX[lab]
        g.append(f'<text x="{xs(i):.1f}" y="{H + VH + 20}" class="ax" text-anchor="middle">{lab.replace("15:25", "15:30")}</text>')
    return (f'<svg viewBox="0 0 {W} {H + VH + 24}" class="ch" role="img">' + "".join(g) + "</svg>")


def daily_svg(d):
    if d.empty:
        return ""
    d = d.copy()
    for n in (25, 50, 75):
        d[f"ma{n}"] = d.close.rolling(n).mean()
    d = d.tail(250)
    W, H, VH = 760, 260, 40
    L, R, T = 8, 58, 10
    pw = W - L - R
    n = len(d)
    step = pw / n
    xs = lambda i: L + (i + 0.5) * step
    lo, hi = d.low.min(), d.high.max()
    pad = (hi - lo) * 0.05
    lo, hi = lo - pad, hi + pad
    y = lambda v: T + (hi - v) / (hi - lo) * (H - T)
    vmax = d.volume.max() or 1
    g = []
    for tv in nice_ticks(lo, hi):
        yy = y(tv)
        g.append(f'<line x1="{L}" x2="{L + pw}" y1="{yy:.1f}" y2="{yy:.1f}" class="grid"/>'
                 f'<text x="{L + pw + 4}" y="{yy + 4:.1f}" class="ax">{fmt(tv)}</text>')
    bw = max(step * 0.6, 1)
    last_m = None
    for i, (t, r) in enumerate(d.iterrows()):
        c = "cu" if r.close >= r.open else "cd"
        x = xs(i)
        g.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{y(r.high):.1f}" y2="{y(r.low):.1f}" class="{c}w"/>')
        top, bot = y(max(r.open, r.close)), y(min(r.open, r.close))
        g.append(f'<rect x="{x - bw / 2:.1f}" y="{top:.1f}" width="{bw:.1f}" height="{max(bot - top, 0.8):.1f}" class="{c}"/>')
        vh = r.volume / vmax * VH
        g.append(f'<rect x="{x - bw / 2:.1f}" y="{H + 6 + VH - vh:.1f}" width="{bw:.1f}" height="{vh:.1f}" class="{c} vol"/>')
        if t.month != last_m:
            if last_m is not None:
                g.append(f'<text x="{x:.1f}" y="{H + VH + 20}" class="ax" text-anchor="middle">{t.month}月</text>')
            last_m = t.month
    for n, c in ((25, "ma25"), (50, "ma50"), (75, "ma75")):
        pts = " ".join(f"{xs(i):.1f},{y(v):.1f}" for i, v in enumerate(d[f"ma{n}"]) if pd.notna(v))
        if pts:
            g.append(f'<polyline points="{pts}" class="{c}"/>')
    return f'<svg viewBox="0 0 {W} {H + VH + 24}" class="ch" role="img">' + "".join(g) + "</svg>"


CSS = """
:root{--bg:#fafaf9;--fg:#1c1c1a;--mut:#6b6b66;--line:#e4e4e0;--card:#fff;--up:#d1343a;--dn:#1f8a55;--acc:#2a5bd7;--nk:#8a6dd8;--band:rgba(234,179,8,.13)}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--fg:#ececea;--mut:#9a9a94;--line:#2c2c2a;--card:#1c1c1b;--up:#f06a6e;--dn:#4cc38a;--acc:#7aa2ff;--nk:#a993f0;--band:rgba(234,179,8,.12)}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,"Hiragino Sans","Yu Gothic UI",sans-serif}
main{max-width:860px;margin:0 auto;padding:20px 16px 60px} h1{font-size:21px;margin:0 0 4px} h2{font-size:16px;margin:26px 0 8px}
.note{color:var(--mut);font-size:12px} a{color:var(--acc)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 10px 4px;margin:0 0 12px}
.hd{display:flex;flex-wrap:wrap;gap:4px 14px;align-items:baseline;font-size:13px;margin:0 2px 4px}
.hd b{font-size:15px} .mut{color:var(--mut)} .up{color:var(--up)} .dn{color:var(--dn)}
.ch{width:100%;height:auto;display:block}
.ch .grid{stroke:var(--line);stroke-width:1} .ch .ax{fill:var(--mut);font-size:11px}
.ch .cu{fill:var(--up)} .ch .cd{fill:var(--dn)} .ch .cuw{stroke:var(--up);stroke-width:1} .ch .cdw{stroke:var(--dn);stroke-width:1}
.ch .vol{opacity:.55} .ch .band{fill:var(--band)} .ch .lunch{stroke:var(--line);stroke-width:1;stroke-dasharray:2 3}
.ch .prev{stroke:var(--mut);stroke-width:1;stroke-dasharray:4 3;opacity:.7}
.ch .nk{fill:none;stroke:var(--nk);stroke-width:1.4;opacity:.75} .ch .lowmk{fill:var(--acc)}
.ch .ma25,.ch .ma50,.ch .ma75{fill:none;stroke-width:1.3} .ch .ma25{stroke:#e0a100} .ch .ma50{stroke:#3b82f6} .ch .ma75{stroke:#e0559a}
.lg{display:inline-block;width:14px;height:3px;vertical-align:middle;margin:0 3px 0 8px;border-radius:2px}
.nav a{margin-right:12px}
"""


def page(title, body):
    now = dt.datetime.utcnow() + dt.timedelta(hours=9)
    return (f'<!doctype html><html lang="ja"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>'
            f'<style>{CSS}</style></head><body><main>{body}'
            f'<p class="note">更新 {now:%Y-%m-%d %H:%M}／データ: Yahoo Finance（5分足）</p></main></body></html>')


def build_stock(code, name, m5, daily, idx5):
    days = sorted(set(m5.index.date), reverse=True)
    closes = daily.close if not daily.empty else pd.Series(dtype=float)
    # 前日終値は日足を優先し、なければ前日の最後の5分足
    last_by_day = m5.groupby(m5.index.date).close.last()
    nav = " ".join(f'<a href="{c}.html">{html.escape(n)}</a>' for c, n in TICKERS.items() if c != code)
    parts = [f'<p class="nav note"><a href="./">一覧</a>{nav}</p>',
             f'<h1>{html.escape(name)}（{code}）</h1>',
             '<h2>日足（約1年）</h2>',
             '<p class="note">移動平均 <span class="lg" style="background:#e0a100"></span>25日'
             '<span class="lg" style="background:#3b82f6"></span>50日<span class="lg" style="background:#e0559a"></span>75日</p>',
             f'<div class="card">{daily_svg(daily)}</div>',
             '<h2>5分足（新しい順）</h2>',
             '<p class="note">薄い紫の線＝日経平均（当日始値からの変化率を、この銘柄の始値にそろえて表示）／'
             '黄色の帯＝10:20〜10:40／点線＝前日終値／青い点＝前場の安値</p>']
    for d in days:
        day = m5[m5.index.date == d]
        idx_day = idx5[idx5.index.date == d] if not idx5.empty else None
        prev = None
        ts = pd.Timestamp(d)
        if not closes.empty and (closes.index < ts).any():
            prev = float(closes[closes.index < ts].iloc[-1])
        elif (last_by_day.index < d).any():
            prev = float(last_by_day[last_by_day.index < d].iloc[-1])
        o, c = day.iloc[0].open, day.iloc[-1].close
        mor = day[day.index.map(lambda t: t.strftime("%-H:%M") in SLOT_IX and SLOT_IX[t.strftime("%-H:%M")] < N_MORNING)]
        low_t = mor.low.idxmin().strftime("%-H:%M") if not mor.empty else "-"
        hd = [f'<b>{d:%m/%d}（{"月火水木金土日"[d.weekday()]}）</b>']
        if prev:
            g, ch = pct(o, prev), pct(c, prev)
            hd.append(f'寄り <span class="{cls(g)}">{g:+.1f}%</span>')
            hd.append(f'終値 <span class="{cls(ch)}">{ch:+.1f}%</span>')
        hd.append(f'<span class="mut">前場安値 {low_t}</span>')
        if idx_day is not None and len(idx_day) > 1:
            nkc = pct(idx_day.iloc[-1].close, idx_day.iloc[0].open)
            hd.append(f'<span class="mut">日経（始値比）</span><span class="{cls(nkc)}">{nkc:+.1f}%</span>')
        parts.append(f'<div class="card"><div class="hd">{"".join(hd)}</div>{intraday_svg(day, idx_day, prev)}</div>')
    return page(f"{name} 5分足", "".join(parts))


def build_index(summ):
    rows = "".join(
        f'<div class="card"><div class="hd"><b><a href="{c}.html">{html.escape(n)}</a></b>'
        f'<span class="mut">{c}</span><span>{fmt(last)}</span><span class="{cls(ch)}">{ch:+.1f}%</span>'
        f'<span class="mut">5分足 {nd}日分</span></div></div>'
        for c, n, last, ch, nd in summ)
    body = ('<h1>5分足の並べ見</h1><p class="note">日足（約1年）と、1日1枚の5分足を新しい順に並べています。'
            '5分足は毎日自動で保存するので、日数は少しずつ増えていきます。</p>' + rows +
            '<p class="note"><a href="../">スクリーナーへ戻る</a></p>')
    return page("5分足の並べ見", body)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    idx5 = merge_store("N225", fetch_5m(INDEX))
    summ = []
    for code, name in TICKERS.items():
        sym = f"{code}.T"
        m5 = merge_store(code, fetch_5m(sym))
        if m5.empty:
            print(f"{code}: 5分足なし")
            continue
        daily = fetch_daily(sym)
        with open(os.path.join(OUT_DIR, f"{code}.html"), "w", encoding="utf-8") as f:
            f.write(build_stock(code, name, m5, daily, idx5))
        last = float(daily.close.iloc[-1]) if len(daily) else float(m5.close.iloc[-1])
        ch = pct(daily.close.iloc[-1], daily.close.iloc[-2]) if len(daily) > 1 else 0.0
        summ.append((code, name, last, ch, len(set(m5.index.date))))
        print(f"{code} {name}: {len(set(m5.index.date))}日分")
    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(build_index(summ))


if __name__ == "__main__":
    main()
