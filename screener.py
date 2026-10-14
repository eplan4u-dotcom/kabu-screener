# -*- coding: utf-8 -*-
"""
東証 全銘柄スクリーナー（ブレイク待ち / 当日ブレイク / 静かな連騰）
引け後（16時以降推奨）に run.bat から実行してください。
"""
import os, re, sys, time, datetime as dt, pickle, urllib.request, urllib.parse, webbrowser, html, json

import numpy as np

import margin
import pattern_d as PD
import watch_ui as WU
import pandas as pd
import yfinance as yf

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get("SCREENER_OUT") or os.path.join(BASE, "output")
CACHE = os.path.join(BASE, "cache")
JPX_PAGE = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"
JPX_FALLBACK = [
    "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx",
    "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls",
]

# ===== 条件（ここを書き換えると基準が変わります） =====
CFG = dict(
    min_turnover=3e8,      # 20日平均の売買代金（円）。これ未満は除外
    min_price=200,         # 株価の下限（円）
    thin_price=1000,       # 「薄」の印：この株価未満で…
    thin_turnover=10e8,    # …20日平均の売買代金がこれ未満（除外はしない。印のみ）
    range_days=60,         # 何日間の高値を「上値（レンジ上限）」とみなすか
    min_base_days=10,      # 上値をつけてから最低何日たっているか（レンジ形成の確認）
    near_pct=5.0,          # ブレイク待ち：上値まで何%以内か
    vol_dry=0.85,          # ブレイク待ち：直近10日の出来高が、その前100日平均の何倍未満か（売り枯れ）
    min_adr=2.5,           # 1日の平均値動き（%）の下限
    breakout_vol=1.5,      # 当日ブレイク：出来高が50日平均の何倍以上か
    streak_min=3,          # 連騰：何日以上の続伸か
    streak_max_gain=5.0,   # 連騰：1日の上昇率の上限（%）。急騰・S高は除外
    gap_max=3.0,           # 連騰：窓開けの上限（%）
    c_max_dev25=8.0,       # 連騰：25日線からの乖離の上限（%）。走り終わった銘柄を除外
    theme_gain=15.0,       # 当日ブレイク：この上昇率（%）以上はテーマ急騰として別枠へ
    theme_vol=10.0,        # 当日ブレイク：出来高が50日平均のこの倍数以上もテーマ急騰として別枠へ
)
# ======================================================

# ===== HYPER SBI 2 取込用CSV =====
HSBI_PER_FILE = 50       # 1リストの上限（HYPER SBI 2 は50銘柄まで）
HSBI_MAX_RATIO = None    # 信用倍率の上限。例: 10 にすると10倍以下だけ（売残0は除外）。None で絞らない
# ===================================


MARGIN, MARGIN_INFO = {}, {}
MARKET = {}   # 地合い（TOPIX連動ETF 1306 の25日線・20日騰落）
BENCH = "1306"
ALL = []   # 売買代金の条件に関係なく、分析できた全銘柄（ウォッチリスト用の all.json に使う）


def log(msg):
    print(msg, flush=True)


def _http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def _jpx_urls():
    """JPXの一覧ページから data_j のリンクを探す（URL変更に自動対応）"""
    urls = []
    try:
        page = _http_get(JPX_PAGE).decode("utf-8", "ignore")
        for href in re.findall(r'href="([^"]*data_j\.(?:xlsx|xls|csv))"', page):
            urls.append(urllib.parse.urljoin(JPX_PAGE, href))
    except Exception as e:
        log(f"  一覧ページの読み込みに失敗: {e}")
    return urls + [u for u in JPX_FALLBACK if u not in urls]


def load_universe():
    """JPXの上場銘柄一覧（data_j）から内国株のコードと銘柄名を取得"""
    os.makedirs(CACHE, exist_ok=True)
    found = [os.path.join(CACHE, f) for f in os.listdir(CACHE) if f.startswith("data_j.")]
    path = max(found, key=os.path.getmtime) if found else None
    fresh = path and (time.time() - os.path.getmtime(path) < 7 * 86400)
    if not fresh:
        log("銘柄一覧をJPXから取得中...")
        err = None
        for url in _jpx_urls():
            try:
                body = _http_get(url)
                path = os.path.join(CACHE, "data_j." + url.rsplit(".", 1)[-1])
                with open(path, "wb") as f:
                    f.write(body)
                err = None
                break
            except Exception as e:
                err = e
        if err and not path:
            log(f"銘柄一覧の取得に失敗しました: {err}")
            log(f"JPXのサイト（{JPX_PAGE}）から一覧ファイルを手動で保存し、{CACHE} に置いてください。")
            sys.exit(1)
        if err:
            log("取得に失敗したため、前回の一覧を使います。")
    if path.endswith(".csv"):
        df = pd.read_csv(path, dtype=str, encoding="cp932")
    else:
        df = pd.read_excel(path, dtype=str)
    mk = df["市場・商品区分"].fillna("")
    df = df[mk.str.contains("内国株式")]
    out = pd.DataFrame({
        "code": df["コード"].str.strip(),
        "name": df["銘柄名"].str.strip(),
        "market": mk[df.index].str.replace("（内国株式）", "", regex=False),
        "sector": df.get("33業種区分", pd.Series("", index=df.index)).fillna(""),
    })
    return out.reset_index(drop=True)


def fetch_prices(codes):
    """Yahoo Financeから日足（約1年分）を取得。当日分はキャッシュ"""
    today = dt.date.today().isoformat()
    cpath = os.path.join(CACHE, f"prices_{today}.pkl")
    if os.path.exists(cpath):
        log("本日取得済みの株価データを使います（取り直す場合は cache フォルダの prices_*.pkl を削除）")
        with open(cpath, "rb") as f:
            return pickle.load(f)
    data = {}
    tickers = [f"{c}.T" for c in codes]
    chunk = 100
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        log(f"株価取得中 {i + len(part)}/{len(tickers)}")
        for attempt in range(3):
            try:
                df = yf.download(part, period="1y", interval="1d", group_by="ticker",
                                 auto_adjust=False, threads=True, progress=False)
                break
            except Exception as e:
                log(f"  再試行 ({e})")
                time.sleep(5)
        else:
            continue
        for t in part:
            try:
                d = df[t] if isinstance(df.columns, pd.MultiIndex) else df
                d = d[["Open", "High", "Low", "Close", "Volume"]].dropna()
                if len(d) > 0:
                    data[t[:-2]] = d
            except Exception:
                pass
        time.sleep(1)
    with open(cpath, "wb") as f:
        pickle.dump(data, f)
    return data


def analyze(d):
    """1銘柄を分析して指標を返す。データ不足ならNone"""
    if len(d) < 80:
        return None
    o, h, l, c, v = (d[k].astype(float).values for k in ["Open", "High", "Low", "Close", "Volume"])
    close = c[-1]
    turnover20 = float(np.mean(c[-20:] * v[-20:]))
    s = pd.Series(c)
    ma5, ma25, ma50, ma75 = (s.rolling(n).mean().values for n in (5, 25, 50, 75))
    adr = float(np.mean((h[-20:] / l[-20:] - 1) * 100))
    vol50 = float(np.mean(v[-51:-1]))
    if vol50 <= 0:
        return None
    vol10 = float(np.mean(v[-10:]))
    vol_prev = float(np.mean(v[-110:-10]))           # 直近10日より前の100日平均（売り枯れ判定の基準）
    vol5 = float(np.mean(v[-5:]))

    rd = CFG["range_days"]
    prior_h = h[-(rd + 1):-1]
    res = float(prior_h.max())
    res_age = rd - int(np.argmax(prior_h))           # 上値をつけてから何日前か
    low10 = float(l[-10:].min())
    hi1y = float(h[-250:-1].max())                    # 前日までの約1年の高値（上にある売り圧力の目安）

    streak, gains, gaps = 0, [], []
    for i in range(len(c) - 1, 0, -1):
        if c[i] > c[i - 1]:
            streak += 1
            gains.append((c[i] / c[i - 1] - 1) * 100)
            gaps.append((o[i] / c[i - 1] - 1) * 100)
        else:
            break

    # D. 押し目からの3連陽（判定は pattern_d.py。検証と同じ）
    dsig = dict(d_ok=False)
    try:
        x = PD.calc(d)
        t = len(c) - 1
        r = PD.judge(x, t, CFG)
        if r["ok"]:
            f = PD.features(x, t, r)
            dsig = {"d_" + k: f[k] for k in ("type", "line", "alert", "stop", "risk", "g3", "ext",
                                              "reclaim25", "vol_dip", "vol_3")}
            dsig.update(d_ok=True, d_since=t - r["touch_i"])
    except Exception:
        pass

    return dict(
        **dsig, ret60=(c[-1] / c[-61] - 1) * 100, ret20=(c[-1] / c[-21] - 1) * 100,
        close=close, chg=(c[-1] / c[-2] - 1) * 100, turnover20=turnover20, adr=adr, adr_yen=close * adr / 100,
        ma5=ma5[-1], ma25=ma25[-1], ma50=ma50[-1], ma75=ma75[-1],
        ma25_up=ma25[-1] > ma25[-6], ma50_up=ma50[-1] >= ma50[-11] * 0.99,   # 50日線が下向きでない
        dev25=(close / ma25[-1] - 1) * 100,
        vol_today=v[-1] / vol50, vol10r=vol10 / vol_prev if vol_prev > 0 else 1.0, vol5r=vol5 / vol50,
        res=res, res_age=res_age, dist=(res / close - 1) * 100, low10=low10,
        hi1y=hi1y, room1y=(hi1y / close - 1) * 100,
        streak=streak, max_gain=max(gains) if gains else 0, max_gap=max(gaps) if gaps else 0,
        spark=[round(x, 2) for x in c[-60:]],
        date=str(d.index[-1].date()),
    )


def market(prices):
    """地合い：TOPIX連動ETF（1306）の終値と25日線・直近20日の騰落"""
    MARKET.clear()
    d = prices.get(BENCH)
    if d is None or len(d) < 30:
        return
    import track
    d = track.adjust_splits(d)
    c = d.Close.astype(float)
    ma25 = float(c.iloc[-25:].mean())
    MARKET.update(date=str(d.index[-1].date()), close=float(c.iloc[-1]), ma25=ma25,
                  above25=bool(c.iloc[-1] > ma25), r20=float((c.iloc[-1] / c.iloc[-21] - 1) * 100))


def market_text():
    """地合いの一行と、A への注意（2025/10〜2026/10 の再現による）"""
    if not MARKET:
        return "", ""
    pos = "25日線の上" if MARKET["above25"] else "25日線の下"
    mark = "○" if MARKET["above25"] else "×"
    sweet = "（過去1年で A〜D とも最も成績が良かった帯）" if 3 <= MARKET["r20"] <= 6 else ""
    line = f"地合い{mark}：TOPIX（1306）は{pos}、直近20日 {MARKET['r20']:+.1f}%{sweet}"
    a_warn = "" if MARKET["above25"] else \
        "今日は地合い×（TOPIXが25日線の下）。過去1年の再現では、この地合いの A は全体を下回った（成功率15%）。"
    return line, a_warn


def sec_txt(r):
    if not _num(r.get("sec_rank")):
        return "-"
    rk = int(r.sec_rank)
    cls = ' class="up"' if rk <= max(1, round(r.sec_n * 0.2)) else ""
    return f'<span{cls}>{rk}位</span><div class="sub">/{int(r.sec_n)}業種</div>'


def screen(universe, prices):
    rows = []
    for _, u in universe.iterrows():
        d = prices.get(u.code)
        if d is None:
            continue
        a = analyze(d)
        if a is None:
            continue
        a.update(code=u.code, name=u["name"], market=u.market, sector=u.sector)
        m = MARGIN.get(u.code) or {}
        a.update(m_ratio=m.get("ratio"), m_buy=m.get("buy"), m_sell=m.get("sell"),
                 m_dbuy=m.get("dbuy"), m_wbuy=m.get("wbuy"), m_wpct=m.get("wpct"))
        ALL.append(a)
        if a["close"] < CFG["min_price"] or a["turnover20"] < CFG["min_turnover"]:
            continue
        rows.append(a)
    df = pd.DataFrame(rows)
    if df.empty:
        return df, df, df, df, df, df

    # A. ブレイク待ち（アラート候補）
    A = df[(df.dist >= 0) & (df.dist <= CFG["near_pct"]) & (df.res_age >= CFG["min_base_days"])
           & (df.vol10r < CFG["vol_dry"]) & (df.adr >= CFG["min_adr"])
           & (df.close > df.ma50) & df.ma50_up].copy()
    A["risk"] = (A.res - A.low10) / A.res * 100          # 上値で買って10日安値で切る場合の損切り幅%
    A["rr"] = A.adr / A.risk.replace(0, np.nan)           # 1日の平均値動きが損切り幅の何倍か
    A = A.sort_values("dist")

    # B. 当日ブレイク
    B = df[(df.close > df.res) & (df.res_age >= CFG["min_base_days"])
           & (df.vol_today >= CFG["breakout_vol"])].copy()
    B["ext"] = (B.close / B.res - 1) * 100
    B["risk"] = (B.close - B.low10) / B.close * 100
    is_theme = (B.chg >= CFG["theme_gain"]) | (B.vol_today >= CFG["theme_vol"])
    T = B[is_theme].sort_values("chg", ascending=False)   # テーマ急騰（別枠）
    B = B[~is_theme].sort_values("vol_today", ascending=False)

    # C. 静かな連騰
    C = df[(df.streak >= CFG["streak_min"]) & (df.max_gain <= CFG["streak_max_gain"])
           & (df.max_gap <= CFG["gap_max"]) & (df.ma5 > df.ma25) & (df.ma25 > df.ma50)
           & df.ma25_up & (df.vol5r >= 1.0) & (df.vol5r <= 3.0)
           & (df.dev25 <= CFG["c_max_dev25"])].copy()
    C = C.sort_values(["streak", "turnover20"], ascending=[False, False])

    # 業種の強さ：対象銘柄の20日騰落の業種中央値の順位（1＝最強。3銘柄未満の業種は順位なし）
    g = df.groupby("sector").ret20.agg(["median", "count"])
    g = g[g["count"] >= 3]
    rank = g["median"].rank(ascending=False, method="min")
    df["sec_rank"] = df.sector.map(rank)
    df["sec_n"] = len(g)
    A["sec_rank"], C["sec_rank"] = df.sec_rank[A.index], df.sec_rank[C.index]
    A["sec_n"] = C["sec_n"] = len(g)

    # D. 押し目からの3連陽（25・50・75日線）
    df["rs60"] = df.ret60 - df.ret60.median()           # 60日騰落の、市場（対象銘柄）の中央値との差
    D = df[df.d_ok == True].copy()   # sec_rank も引き継ぐ
    if len(D):
        view = pd.DataFrame({"type": D.d_type, "reclaim25": D.d_reclaim25.astype(bool), "vol_3": D.d_vol_3,
                             "rs60": D.rs60, "room1y": D.room1y})
        D = D[PD.prod_mask(view).values].sort_values("rs60", ascending=False)
    return df, A, B, C, T, D


def _num(x):
    return x is not None and not (isinstance(x, float) and np.isnan(x))


def man(x, sign=True):
    """株数を「万」表記に（例: -66700 → -6.7万）"""
    if not _num(x):
        return "-"
    x = float(x)
    s = "+" if sign and x > 0 else ""
    return f"{s}{x / 1e4:,.1f}万" if abs(x) >= 10000 else f"{s}{x:,.0f}"


def ratio_txt(r):
    if not _num(r.m_buy):
        return "-"
    if not _num(r.m_ratio):
        return '<span class="sub">売残0</span>'
    cls = ' class="warn"' if r.m_ratio >= 5 else ""
    return f'<span{cls}>{r.m_ratio:.2f}倍</span>'


def buychg_txt(r):
    if not _num(r.m_buy):
        return "-"
    w = ""
    if _num(r.m_wbuy):
        pct = f"（{r.m_wpct:+.0f}%）" if _num(r.m_wpct) else ""
        cls = ' class="warn"' if _num(r.m_wpct) and r.m_wpct >= 10 else ""
        w = f'<span{cls}>週 {man(r.m_wbuy)}{pct}</span>'
    return f'{w}<div class="sub">前日 {man(r.m_dbuy)}・残 {man(r.m_buy, False)}</div>'


def margin_plain(r):
    """メール（テキスト）用の一行"""
    if not _num(r.m_buy):
        return ""
    ratio = f"{r.m_ratio:.2f}倍" if _num(r.m_ratio) else "売残0"
    w = f" 週{man(r.m_wbuy)}" if _num(r.m_wbuy) else ""
    return f" / 信用倍率{ratio} 買残{w} 前日{man(r.m_dbuy)}"


def margin_html(r):
    """メール（HTML）用"""
    if not _num(r.m_buy):
        return ""
    if _num(r.m_ratio):
        col = "#d97706" if r.m_ratio >= 5 else "#444"
        ratio = f'<span style="color:{col};">信用倍率 {r.m_ratio:.2f}倍</span>'
    else:
        ratio = '信用倍率 売残0'
    w = ""
    if _num(r.m_wbuy):
        col = "#d97706" if _num(r.m_wpct) and r.m_wpct >= 10 else "#444"
        pct = f"（{r.m_wpct:+.0f}%）" if _num(r.m_wpct) else ""
        w = f'　<span style="color:{col};">買残 週{man(r.m_wbuy)}{pct}</span>'
    return f'<span style="font-size:13px;">{ratio}{w}<span style="color:#999;">　前日{man(r.m_dbuy)}</span></span>'


def room_txt(r):
    """1年高値までの余裕。上に売り圧力が近いと注意表示"""
    if r.room1y <= 0:
        return '<span class="up">1年高値更新</span>'
    cls = ' class="warn"' if r.room1y < 5 else ""
    return f'<span{cls}>{fmt(r.room1y, 1, "%")}</span><div class="sub">{fmt(r.hi1y, 0)}</div>'


# ---------- HTML出力 ----------
def spark_svg(vals, line=None):
    w, hgt = 120, 32
    lo, hi = min(vals + ([line] if line else [])), max(vals + ([line] if line else []))
    rng = (hi - lo) or 1
    pts = " ".join(f"{i * w / (len(vals) - 1):.1f},{hgt - (x - lo) / rng * hgt:.1f}" for i, x in enumerate(vals))
    ref = ""
    if line:
        y = hgt - (line - lo) / rng * hgt
        ref = f'<line x1="0" x2="{w}" y1="{y:.1f}" y2="{y:.1f}" stroke="#e5484d" stroke-dasharray="3 2" stroke-width="1"/>'
    col = "#e5484d" if vals[-1] >= vals[0] else "#30a46c"
    return (f'<svg width="{w}" height="{hgt}" viewBox="0 0 {w} {hgt}">{ref}'
            f'<polyline fill="none" stroke="{col}" stroke-width="1.5" points="{pts}"/></svg>')


def shikiho(code):
    return f"https://shikiho.toyokeizai.net/chart/{code}"


def shikiho_co(code):
    return f"https://shikiho.toyokeizai.net/stocks/{code}"


def tv(code):
    return f"https://jp.tradingview.com/chart/?symbol=TSE:{code}"


def is_thin(r):
    """低位で売買代金が小さい（板が薄く値が飛びやすい）。2026-10-13 の再現では成績はむしろ良かったので、除外せず印だけ"""
    return _num(r.get("turnover20")) and r.close < CFG["thin_price"] and r.turnover20 < CFG["thin_turnover"]


def name_cell(r):
    """1行目: 銘柄名（四季報企業ページ）／2行目: コード（TradingView）・業種・株探"""
    sector = html.escape(str(r.sector)).replace("・", "")
    return (f'{WU.star(r.code)}<a class="nm" href="{shikiho_co(r.code)}" target="_blank">{html.escape(r["name"])}</a>'
            + (' <span class="thin">薄</span>' if is_thin(r) else '') +
            f'<div class="sub"><a class="nm" href="{tv(r.code)}" target="_blank">{r.code}</a>・{sector}・'
            f'<a href="https://kabutan.jp/stock/chart?code={r.code}" target="_blank">株探</a></div>')


def links(code):
    return (f'<a href="https://kabutan.jp/stock/chart?code={code}" target="_blank">株探</a> '
            f'<a href="https://jp.tradingview.com/chart/?symbol=TSE:{code}" target="_blank">TV</a>')


def fmt(x, nd=1, suf=""):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "-"
    return f"{x:,.{nd}f}{suf}"


def table(df, cols, line_col=None):
    if df.empty:
        return '<p class="empty">該当なし</p>'
    head = "".join(f'<th onclick="sortT(this)">{html.escape(c[0])}</th>' for c in cols)
    body = []
    for _, r in df.iterrows():
        tds = []
        for title, fn, key in cols:
            val = r[key] if key and key in r else ""
            tds.append(f'<td data-v="{val if isinstance(val, (int, float, np.floating, np.integer)) else html.escape(str(val))}">{fn(r)}</td>')
        body.append("<tr>" + "".join(tds) + "</tr>")
    return f'<div class="wrap"><table><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def margin_note():
    if not MARGIN_INFO:
        return "信用残は取得できませんでした。"
    base = f"、週＝{MARGIN_INFO['base']}比" if MARGIN_INFO.get("base") else ""
    return f"信用残はJPX公表 {MARGIN_INFO['date']}申込現在（前日比{base}）。信用倍率5倍以上・買残が週+10%以上はオレンジ。"


def d_type_txt(r):
    sub = "25日線を奪回" if r.d_type != "25" else ""
    return f'<b>{r.d_type}日線</b>' + (f'<div class="sub">{sub}</div>' if sub else "")


def d_vol_txt(r):
    dry = r.d_vol_dip < PD.DRY
    head = f'<span class="up">売り枯れ</span> ' if dry else ""
    return f'{head}押し目 {fmt(r.d_vol_dip, 2, "倍")}<div class="sub">3連陽 {fmt(r.d_vol_3, 2, "倍")}</div>'


def write_html(df, A, B, C, T, D):
    os.makedirs(OUT, exist_ok=True)
    date = df.date.max() if not df.empty else dt.date.today().isoformat()
    base = [
        ("銘柄", name_cell, "code"),
        ("終値", lambda r: fmt(r.close, 1), "close"),
        ("前日比", lambda r: f'<span class="{"up" if r.chg >= 0 else "dn"}">{fmt(r.chg, 2, "%")}</span>', "chg"),
    ]
    tail = [
        ("60日", lambda r: f'<a href="{tv(r.code)}" target="_blank" class="spark">{spark_svg(r.spark, r.res)}</a>', None),
    ]
    colsA = base + [
        ("アラート", lambda r: f"<b>{fmt(r.res, 1)}</b>", "res"),
        ("あと", lambda r: fmt(r.dist, 1, "%"), "dist"),
        ("経過", lambda r: f"{int(r.res_age)}日", "res_age"),
        ("出来高比", lambda r: fmt(r.vol10r, 2, "倍"), "vol10r"),
        ("値幅", lambda r: f'{fmt(r.adr_yen, 0)}円<div class="sub">{fmt(r.adr, 1, "%")}</div>', "adr_yen"),
        ("業種順位", sec_txt, "sec_rank"),
        ("信用倍率", ratio_txt, "m_ratio"),
        ("買残", buychg_txt, "m_wpct"),
        ("年高まで", room_txt, "room1y"),
    ] + tail
    colsB = base + [
        ("抜けた上値", lambda r: fmt(r.res, 1), "res"),
        ("上値からの上昇", lambda r: fmt(r.ext, 1, "%"), "ext"),
        ("出来高比", lambda r: fmt(r.vol_today, 1, "倍"), "vol_today"),
        ("値幅", lambda r: f'{fmt(r.adr_yen, 0)}円<div class="sub">{fmt(r.adr, 1, "%")}</div>', "adr_yen"),
        ("信用倍率", ratio_txt, "m_ratio"),
        ("買残", buychg_txt, "m_wpct"),
        ("年高まで", room_txt, "room1y"),
    ] + tail
    colsT = base + [
        ("抜けた上値", lambda r: fmt(r.res, 1), "res"),
        ("出来高比", lambda r: fmt(r.vol_today, 1, "倍"), "vol_today"),
        ("25日線乖離", lambda r: fmt(r.dev25, 1, "%"), "dev25"),
        ("信用倍率", ratio_txt, "m_ratio"),
        ("買残", buychg_txt, "m_wpct"),
        ("年高まで", room_txt, "room1y"),
    ] + tail
    colsD = base + [
        ("型", d_type_txt, "d_type"),
        ("アラート", lambda r: f'<b>{fmt(r.d_alert, 1)}</b><div class="sub">あと {fmt((r.d_alert / r.close - 1) * 100, 1, "%")}</div>', "d_alert"),
        ("損切り目安", lambda r: f'{fmt(r.d_stop, 1)}<div class="sub">-{fmt(r.d_risk, 1, "%")}</div>', "d_risk"),
        ("3日上昇", lambda r: f'<span class="{"warn" if r.d_ext else ""}">{fmt(r.d_g3, 1, "%")}</span>', "d_g3"),
        ("相対強さ", lambda r: f"{r.rs60:+.0f}%", "rs60"),
        ("業種順位", sec_txt, "sec_rank"),
        ("出来高", d_vol_txt, "d_vol_dip"),
        ("信用倍率", ratio_txt, "m_ratio"),
        ("買残", buychg_txt, "m_wpct"),
        ("年高まで", room_txt, "room1y"),
        ("60日", lambda r: f'<a href="{tv(r.code)}" target="_blank" class="spark">{spark_svg(r.spark, r.d_line)}</a>', None),
    ]
    colsC = base + [
        ("連騰", lambda r: f"<b>{int(r.streak)}日</b>", "streak"),
        ("最大上昇/日", lambda r: fmt(r.max_gain, 1, "%"), "max_gain"),
        ("25日線乖離", lambda r: fmt(r.dev25, 1, "%"), "dev25"),
        ("出来高比", lambda r: fmt(r.vol5r, 2, "倍"), "vol5r"),
        ("業種順位", sec_txt, "sec_rank"),
        ("あと", lambda r: fmt(r.dist, 1, "%") if r.dist >= 0 else "高値更新中", "dist"),
        ("信用倍率", ratio_txt, "m_ratio"),
        ("買残", buychg_txt, "m_wpct"),
        ("年高まで", room_txt, "room1y"),
    ] + tail

    mline, awarn = market_text()
    mcls = "mk-ok" if MARKET.get("above25") else "mk-ng"
    mbox = f'<p class="mk {mcls}">{html.escape(mline)}</p>' if mline else ""
    awarn_html = f'<p class="note warn">{html.escape(awarn)}</p>' if awarn else ""
    cfg_txt = (f"売買代金{CFG['min_turnover']/1e8:.0f}億円以上・株価{CFG['min_price']}円以上／"
               f"上値＝過去{CFG['range_days']}日高値（{CFG['min_base_days']}日以上前）／"
               f"「薄」＝株価{CFG['thin_price']:,}円未満かつ売買代金{CFG['thin_turnover']/1e8:.0f}億円未満（板が薄く値が飛びやすい。除外はしていない）")
    page = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>スクリーナー {date}</title>
<style>
:root{{--bg:#fafaf9;--fg:#1c1c1a;--mut:#6b6b66;--line:#e4e4e0;--card:#fff;--up:#d1343a;--dn:#1f8a55;--acc:#2a5bd7}}
@media (prefers-color-scheme:dark){{:root{{--bg:#141413;--fg:#ececea;--mut:#9a9a94;--line:#2c2c2a;--card:#1c1c1b;--up:#f06a6e;--dn:#4cc38a;--acc:#7aa2ff}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 "Yu Gothic UI","Meiryo",sans-serif}}
main{{max-width:1400px;margin:0 auto;padding:20px 16px 60px}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:17px;margin:32px 0 4px}}
.note{{color:var(--mut);font-size:13px;margin:0 0 10px}}
.tabs a{{display:inline-block;margin:10px 8px 0 0;padding:6px 12px;border:1px solid var(--line);border-radius:999px;color:var(--fg);text-decoration:none;background:var(--card)}}
.wrap{{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--card)}}
table{{border-collapse:collapse;width:100%;white-space:nowrap}}
th,td{{padding:7px 10px;border-bottom:1px solid var(--line);text-align:right;vertical-align:middle}}
th{{position:sticky;top:0;background:var(--card);font-size:12px;color:var(--mut);cursor:pointer;user-select:none}}
td:nth-child(1),th:nth-child(1){{text-align:left;position:sticky;left:0;z-index:2;background:var(--card);
  box-shadow:6px 0 6px -6px rgba(0,0,0,.35);min-width:8.5em;max-width:11em;white-space:normal;line-height:1.35}}
th:nth-child(1){{z-index:3}}
td:nth-child(1) .sub{{white-space:nowrap}}
a.spark{{display:inline-block;padding:4px 0}} a.spark svg{{display:block}}
.sub{{font-size:11px;color:var(--mut)}} .thin{{font-size:11px;padding:0 4px;border:1px solid var(--mut);border-radius:4px;color:var(--mut);white-space:nowrap}} .up{{color:var(--up)}} .dn{{color:var(--dn)}}
a{{color:var(--acc)}} a.nm{{color:inherit;text-decoration:underline;text-decoration-color:var(--line);text-underline-offset:3px}} .empty{{color:var(--mut)}} .warn{{color:#d97706;font-weight:600}}
.mk{{margin:8px 0 4px;padding:8px 12px;border-radius:8px;font-weight:600;font-size:14px}} .mk-ok{{background:rgba(209,52,58,.10);color:var(--up)}} .mk-ng{{background:rgba(31,138,85,.12);color:var(--dn)}}
{WU.CSS}
</style></head><body><main>
<h1>東証スクリーナー　{date}</h1>
{mbox}
<p class="note"><a href="track/">抽出結果のその後（成績ページ）</a></p>
<p class="note">対象 {len(df):,}銘柄（{html.escape(cfg_txt)}）。見出しをクリックで並べ替え。{html.escape(margin_note())}</p>
<div class="tabs"><a id="wtab" href="#w">★ ウォッチ</a><a href="#a">A ブレイク待ち {len(A)}</a><a href="#b">B 当日ブレイク {len(B)}</a><a href="#c">C 静かな連騰 {len(C)}</a><a href="#d">D 押し目3連陽 {len(D)}</a><a href="#t">別枠 テーマ急騰 {len(T)}</a></div>

{WU.HTML}

<h2 id="a">A. ブレイク待ち（アラート候補） {len(A)}銘柄</h2>
<p class="note">上値の{CFG['near_pct']:.0f}%以内・出来高が細っている・50日線の上。上値の価格にアラートを置く候補。赤の点線が上値。業種順位は業種の直近20日騰落の順位（上位20%は赤）。</p>
{awarn_html}
{table(A, colsA)}

<h2 id="b">B. 当日ブレイク {len(B)}銘柄</h2>
<p class="note">終値で上値を超え、出来高が50日平均の{CFG['breakout_vol']}倍以上。「上値からの上昇」が大きいほど、今から買うと損切り幅が広がる。「1年高値まで」がオレンジ（5%未満）は、すぐ上に売り圧力あり。</p>
{table(B, colsB)}

<h2 id="c">C. 静かな連騰 {len(C)}銘柄</h2>
<p class="note">{CFG['streak_min']}日以上続伸・1日+{CFG['streak_max_gain']:.0f}%以内・窓開け{CFG['gap_max']:.0f}%以内・5日＞25日＞50日線・出来高は平常の1〜3倍・25日線から{CFG['c_max_dev25']:.0f}%以内（走り終わりを除外）。</p>
{table(C, colsC)}

<h2 id="d">D. 押し目からの3連陽 {len(D)}銘柄</h2>
<p class="note">上昇トレンド中（25日線＞75日線）に、安値が25日・50日・75日線のどれかまで下がり（少しの割り込みは可）、そこから陽線3本・安値切り上げで離れた銘柄。50・75日線型は25日線を奪回済み、3連陽の出来高は平常の1.5倍未満、60日の値動きが市場の中央値より強く、1年高値まで20%以内。相対強さの順。業種順位は業種の直近20日騰落の順位（強い業種の D は全体比で成績が良いが、業種の勢いによる分が大きい）。アラート＝3本目の高値、損切り目安＝タッチから3本目までの最安値。赤の点線は触れた線。3日上昇のオレンジは走りすぎ（損切り幅が広い）。「売り枯れ」は押し目の出来高が平常の{PD.DRY}倍未満（参考の印）。{d_note(D)}</p>
{table(D, colsD)}

<h2 id="t">別枠. テーマ急騰 {len(T)}銘柄</h2>
<p class="note">上値は抜けたが、+{CFG['theme_gain']:.0f}%以上または出来高が平常の{CFG['theme_vol']:.0f}倍以上の急騰。ブレイク手法の対象外として参考表示。</p>
{table(T, colsT)}
</main>
<script>
function sortT(th){{const t=th.closest('table'),i=[...th.parentNode.children].indexOf(th),b=t.tBodies[0];
const asc=th.dataset.asc!=='1';th.dataset.asc=asc?'1':'0';
const rows=[...b.rows].sort((x,y)=>{{let a=x.cells[i].dataset.v,c=y.cells[i].dataset.v;const na=parseFloat(a),nc=parseFloat(c);
if(!isNaN(na)&&!isNaN(nc))return asc?na-nc:nc-na;return asc?String(a).localeCompare(c,'ja'):String(c).localeCompare(a,'ja');}});
rows.forEach(r=>b.appendChild(r));}}
{WU.JS}

</script></body></html>"""
    path = os.path.join(OUT, f"screen_{date}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    with open(os.path.join(OUT, "latest.html"), "w", encoding="utf-8") as f:
        f.write(page)
    drop = ["spark"]
    for name, x in (("A_breakout_watch", A), ("B_breakout_today", B), ("C_quiet_streak", C), ("D_pullback_3up", D),
                    ("T_theme_spike", T)):
        x.drop(columns=drop, errors="ignore").to_csv(os.path.join(OUT, f"{name}_{date}.csv"),
                                                     index=False, encoding="utf-8-sig")
    write_hypersbi(date, A, B, C, D, T)
    write_summary(df, A, B, C, T, D, date)
    write_all_json(date, A, B, C, D, T)
    return path


def write_hypersbi(date, A, B, C, D, T):
    """A→B→C→D→別枠の順に連結し、重複を除いて50銘柄ごとに HYPER SBI 2 の銘柄リスト形式で出力。
    形式は HYPER SBI 2 のエクスポートと同じ「コード,銘柄名,TKY,,,,,,メモ」（9列目がメモ欄）（Shift_JIS・CRLF・見出しなし）"""
    d = os.path.join(OUT, "hypersbi")
    os.makedirs(d, exist_ok=True)
    for f in os.listdir(d):   # 前回分は消して、常に最新だけ置く（過去分はメールと git 履歴に残る）
        if f.endswith(".csv"):
            os.remove(os.path.join(d, f))
    def memo(k, r):   # メモ欄（9列目）。カンマは区切りと衝突するので金額は桁区切りなし
        try:
            if k == "A":
                return f"A ブレイク待ち アラート{r.res:.0f}"
            if k == "B":
                return f"B 当日ブレイク 上値{r.res:.0f}"
            if k == "C":
                return f"C {int(r.streak)}日連騰"
            if k == "D":
                return f"D {r.d_type}日線3連陽 アラート{r.d_alert:.0f} 損切{r.d_stop:.0f}"
            return "別枠 テーマ急騰"
        except Exception:
            return k
    rows, seen = [], set()
    for k, x in (("A", A), ("B", B), ("C", C), ("D", D), ("T", T)):
        for _, r in x.iterrows():
            code = str(r.code)
            if code in seen:
                continue
            if HSBI_MAX_RATIO is not None:
                ratio = r.get("m_ratio")
                if _num(ratio):
                    if ratio > HSBI_MAX_RATIO:
                        continue
                elif _num(r.get("m_buy")) and r.get("m_buy", 0) > 0:   # 売残0（倍率が出ない）は除外
                    continue
            seen.add(code)
            name = str(r["name"]).replace(",", "，").replace('"', "")
            rows.append(f"{code},{name},TKY,,,,,,{memo(k, r).replace(',', '')}")
    n = max(1, -(-len(rows) // HSBI_PER_FILE))
    for i in range(n):
        part = rows[i * HSBI_PER_FILE:(i + 1) * HSBI_PER_FILE]
        if not part:
            break
        suf = f"_{i + 1}of{n}" if n > 1 else ""
        with open(os.path.join(d, f"hypersbi_{date}{suf}.csv"), "w", encoding="cp932", errors="replace",
                  newline="") as f:
            f.write("\r\n".join(part) + "\r\n")
    log(f"HYPER SBI 2 用CSV: {len(rows)}銘柄・{n if rows else 0}ファイル")


def write_all_json(date, A, B, C, D, T):
    """ウォッチリスト用：全銘柄の今日の指標（公開データのみ。個人の銘柄リストは含まない）"""
    lists = {}
    for key, x in (("A", A), ("B", B), ("C", C), ("D", D), ("T", T)):
        for code in x.code if len(x) else []:
            lists[code] = lists.get(code, "") + key

    def r1(v, nd=1):
        return None if not _num(v) else round(float(v), nd)
    recs = []
    for a in ALL:
        recs.append(dict(
            c=a["code"], n=a["name"], s=a["sector"], p=r1(a["close"]), ch=r1(a["chg"], 2),
            m25=r1(a["ma25"]), m50=r1(a["ma50"]), m75=r1(a["ma75"]), l10=r1(a["low10"]),
            res=r1(a["res"]), hy=r1(a["hi1y"]), adr=r1(a["adr"]), vr=r1(a["vol_today"], 2),
            to=r1(a["turnover20"] / 1e8, 1), L=lists.get(a["code"], ""),
            da=r1(a.get("d_alert")), dst=r1(a.get("d_stop")), dt=a.get("d_type") if a.get("d_ok") else None,
            mr=r1(a.get("m_ratio"), 2), mw=r1(a.get("m_wpct"), 0), mwb=r1(a.get("m_wbuy"), 0),
            md=r1(a.get("m_dbuy"), 0), mb=r1(a.get("m_buy"), 0),
        ))
        if a["turnover20"] >= CFG["min_turnover"]:     # 容量を抑えるため、60日チャートは対象銘柄のみ
            nd = 0 if a["close"] >= 1000 else 1
            recs[-1]["sp"] = [r1(v, nd) for v in a["spark"]]
    os.makedirs(os.path.join(OUT, "data"), exist_ok=True)
    # Google スプレッドシートのウォッチリストが IMPORTDATA で銘柄名を引くための一覧
    with open(os.path.join(OUT, "data", "names.csv"), "w", encoding="utf-8") as f:
        f.write("code,name\n" + "".join(f'{a["code"]},"{str(a["name"]).replace(chr(34), "")}"\n' for a in ALL))
    with open(os.path.join(OUT, "data", "all.json"), "w", encoding="utf-8") as f:
        json.dump(dict(date=date, margin=MARGIN_INFO.get("date"), stocks=recs), f, ensure_ascii=False,
                  separators=(",", ":"))


PAGE_URL = "https://eplan4u-dotcom.github.io/kabu-screener/"


def d_note(D):
    """件数による注意書き（2025/10〜2026/10 の再現：1日1〜3件の日は成功率38%、全体比+16pt）"""
    if 1 <= len(D) <= 3:
        return "　★今日は件数が少ない日（1〜3件）。過去1年の再現では、こうした日の D は成績が特に良かった。"
    if len(D) > 20:
        return "　※今日は件数が多く、地合いの一斉反発の可能性。個別の強さは割り引いて見る。"
    return ""


def write_summary(df, A, B, C, T, D, date):
    """メール本文（テキスト）を書き出す"""
    def name(r):
        return f"{r.code} {r['name']}（{r.get('sector', '')}）" + ("［薄］" if is_thin(r) else "")

    def room(r):
        return "1年高値更新" if r.room1y <= 0 else f"1年高値まで{r.room1y:.1f}%"

    mline, awarn = market_text()
    L = [f"東証スクリーナー {date}（対象 {len(df):,}銘柄）"] + ([mline] if mline else []) + ["", f"結果ページ: {PAGE_URL}", ""]
    L.append(f"■ A. ブレイク待ち（アラート候補） {len(A)}銘柄")
    if awarn:
        L.append(f"  ※{awarn}")
    for _, r in A.iterrows():
        L.append(f"・{name(r)}  終値{r.close:,.0f} → アラート {r.res:,.0f}（あと{r.dist:.1f}%）{margin_plain(r)}")
    L += ["", f"■ B. 当日ブレイク {len(B)}銘柄"]
    for _, r in B.iterrows():
        L.append(f"・{name(r)}  終値{r.close:,.0f}（{r.chg:+.1f}%）上値{r.res:,.0f}を突破 / 出来高{r.vol_today:.1f}倍 / {room(r)}{margin_plain(r)}")
    L += ["", f"■ C. 静かな連騰 {len(C)}銘柄"]
    for _, r in C.iterrows():
        L.append(f"・{name(r)}  終値{r.close:,.0f}  {int(r.streak)}日連騰 / 25日線+{r.dev25:.1f}%{margin_plain(r)}")
    L += ["", f"■ D. 押し目からの3連陽 {len(D)}銘柄{d_note(D)}"]
    for _, r in D.iterrows():
        dry = " 売り枯れ" if r.d_vol_dip < PD.DRY else ""
        sec = f" 業種{int(r.sec_rank)}位/{int(r.sec_n)}" if _num(r.get("sec_rank")) else ""
        L.append(f"・{name(r)}  {r.d_type}日線から3連陽{sec} → アラート {r.d_alert:,.0f}（あと{(r.d_alert / r.close - 1) * 100:.1f}%）"
                 f" 損切り目安{r.d_stop:,.0f}（-{r.d_risk:.1f}%）{dry}{margin_plain(r)}")
    if len(T):
        L += ["", f"■ 別枠. テーマ急騰 {len(T)}銘柄（手法の対象外）"]
        for _, r in T.iterrows():
            L.append(f"・{name(r)}  終値{r.close:,.0f}（{r.chg:+.1f}%）")
    L += ["", margin_note(), "企業ページ（会社四季報）: https://shikiho.toyokeizai.net/stocks/コード",
          f"抽出結果のその後（成績ページ）: {PAGE_URL}track/"]
    with open(os.path.join(OUT, "summary.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    write_summary_html(df, A, B, C, T, D, date)
    with open(os.path.join(OUT, "subject.txt"), "w", encoding="utf-8") as f:
        mk = ("地合い○ " if MARKET.get("above25") else "地合い× ") if MARKET else ""
        f.write(f"スクリーナー {date}  {mk}A{len(A)} / B{len(B)} / C{len(C)} / D{len(D)}")


def write_summary_html(df, A, B, C, T, D, date):
    """読みやすいHTMLメール本文。1銘柄＝2行、銘柄名は四季報チャートへのリンク"""
    E = html.escape
    sec_css = "margin:28px 0 6px;padding:8px 12px;border-radius:8px;font-size:16px;font-weight:700;"
    def head(title, n, color, bg, note):
        return (f'<div style="{sec_css}color:{color};background:{bg};">{E(title)}　{n}銘柄</div>'
                f'<div style="font-size:12px;color:#888;margin:0 0 4px 2px;">{E(note)}</div><br>')

    def item(r, line1):
        """1行目: 銘柄名・コード・業種＋内容／2行目: 信用倍率…／銘柄間は空行"""
        m = margin_html(r)
        return (f'<a href="{shikiho_co(r.code)}" style="color:#1a56db;text-decoration:none;font-weight:600;">{E(r["name"])}</a>'
                + (' <span style="font-size:11px;padding:0 4px;border:1px solid #999;border-radius:4px;color:#888;">薄</span>' if is_thin(r) else '')
                + f' <a href="{tv(r.code)}" style="color:#888;font-size:13px;text-decoration:none;">{r.code}</a>'
                f'<span style="color:#888;font-size:13px;">・{E(str(r.get("sector", "")))}</span>　{line1}'
                + (f'<br>{m}' if m else '') + '<br><br>')

    def room(r):
        if r.room1y <= 0:
            return '<span style="color:#d1343a;">1年高値更新</span>'
        col = "#d97706" if r.room1y < 5 else "#666"
        return f'<span style="color:{col};">1年高値まで{r.room1y:.1f}%</span>'

    parts = [f'<div style="font-family:-apple-system,\'Hiragino Sans\',sans-serif;max-width:560px;margin:0 auto;padding:4px 8px;color:#222;">',
             f'<div style="font-size:20px;font-weight:700;">東証スクリーナー {date}</div>',
             f'<div style="font-size:13px;color:#888;margin:4px 0 6px;">対象 {len(df):,}銘柄</div>',
             (f'<div style="font-size:14px;font-weight:700;margin:0 0 14px;color:{"#d1343a" if MARKET.get("above25") else "#1f8a55"};">{E(market_text()[0])}</div>'
              if MARKET else ""),
             f'<a href="{PAGE_URL}" style="display:inline-block;background:#1a56db;color:#fff;text-decoration:none;'
             'padding:10px 18px;border-radius:8px;font-size:15px;font-weight:600;">結果ページを開く</a><br>']
    parts.append(head("A. ブレイク待ち", len(A), "#1e3a8a", "#e8efff", "上値にアラートを置く候補（上値に近い順）"))
    if market_text()[1]:
        parts.append(f'<div style="font-size:13px;color:#d97706;font-weight:600;margin:-6px 0 10px 2px;">{E(market_text()[1])}</div>')
    for _, r in A.iterrows():
        parts.append(item(r, f'アラート <b>{r.res:,.0f}</b>　あと{r.dist:.1f}%'
                             f'<span style="color:#999;">（終値 {r.close:,.0f}）</span>'))
    parts.append(head("B. 当日ブレイク", len(B), "#7c2d12", "#fff1e6", "上値を出来高を伴って抜けた銘柄"))
    for _, r in B.iterrows():
        parts.append(item(r, f'終値 <b>{r.close:,.0f}</b> <span style="color:#d1343a;">{r.chg:+.1f}%</span>'
                             f'　上値 {r.res:,.0f} 突破　出来高 {r.vol_today:.1f}倍　{room(r)}'))
    parts.append(head("C. 静かな連騰", len(C), "#14532d", "#e9f7ee", "急騰なしで続伸・25日線から近い"))
    for _, r in C.iterrows():
        parts.append(item(r, f'終値 <b>{r.close:,.0f}</b>　{int(r.streak)}日連騰　25日線 +{r.dev25:.1f}%'))
    parts.append(head("D. 押し目からの3連陽", len(D), "#5b21b6", "#f1ebff",
                      "25・50・75日線まで押して、陽線3本で離れた銘柄（相対強さ順）" + d_note(D)))
    for _, r in D.iterrows():
        dry = '　<span style="color:#d1343a;">売り枯れ</span>' if r.d_vol_dip < PD.DRY else ""
        sec = f'　<span style="color:#888;">業種{int(r.sec_rank)}位</span>' if _num(r.get("sec_rank")) else ""
        parts.append(item(r, f'{r.d_type}日線から3連陽{sec}　アラート <b>{r.d_alert:,.0f}</b>'
                             f'<span style="color:#999;">（あと{(r.d_alert / r.close - 1) * 100:.1f}%）</span>'
                             f'　損切り目安 {r.d_stop:,.0f}（-{r.d_risk:.1f}%）{dry}'))
    if len(T):
        parts.append(head("別枠. テーマ急騰", len(T), "#555", "#f1f1f1", "手法の対象外（参考）"))
        for _, r in T.iterrows():
            parts.append(item(r, f'終値 {r.close:,.0f} <span style="color:#d1343a;">{r.chg:+.1f}%</span>'))
    parts.append(f'<div style="font-size:12px;color:#999;margin-top:24px;">{E(margin_note())}</div>')
    parts.append(f'<div style="font-size:13px;margin-top:10px;"><a href="{PAGE_URL}track/" style="color:#1a56db;">抽出結果のその後（成績ページ）</a></div>')
    parts.append('<div style="font-size:12px;color:#999;margin-top:6px;">銘柄名をタップすると会社四季報の企業ページ、コードをタップするとTradingViewのチャートが開きます。</div></div>')
    with open(os.path.join(OUT, "summary.html"), "w", encoding="utf-8") as f:
        f.write("".join(parts))


def main():
    t0 = time.time()
    uni = load_universe()
    log(f"対象銘柄: {len(uni):,}")
    prices = fetch_prices(uni.code.tolist() + [BENCH])
    market(prices)
    if MARKET:
        log(market_text()[0])
    log("信用残を取得中...")
    try:
        m, info = margin.load(log)
        MARGIN.update(m)
        MARGIN_INFO.update(info)
        log(f"信用残: {len(m):,}銘柄（{info.get('date')}）")
    except Exception as e:
        log(f"信用残の取得に失敗（表示なしで続行）: {e}")
    log(f"株価データ取得: {len(prices):,}銘柄")
    df, A, B, C, T, D = screen(uni, prices)
    if df.empty:
        log("分析できる銘柄がありませんでした。ネット接続を確認してください。")
        return
    path = write_html(df, A, B, C, T, D)
    log(f"\n完了（{time.time() - t0:.0f}秒）  A:{len(A)}  B:{len(B)}  C:{len(C)}  D:{len(D)}  テーマ:{len(T)}")
    log(f"結果: {path}")
    if "--no-open" not in sys.argv:
        webbrowser.open("file:///" + path.replace("\\", "/"))


if __name__ == "__main__":
    main()
