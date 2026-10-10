# -*- coding: utf-8 -*-
"""
東証 全銘柄スクリーナー（ブレイク待ち / 当日ブレイク / 静かな連騰）
引け後（16時以降推奨）に run.bat から実行してください。
"""
import os, sys, time, datetime as dt, pickle, urllib.request, webbrowser, html, json

import numpy as np
import pandas as pd
import yfinance as yf

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get("SCREENER_OUT") or os.path.join(BASE, "output")
CACHE = os.path.join(BASE, "cache")
JPX_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx"

# ===== 条件（ここを書き換えると基準が変わります） =====
CFG = dict(
    min_turnover=3e8,      # 20日平均の売買代金（円）。これ未満は除外
    min_price=200,         # 株価の下限（円）
    range_days=60,         # 何日間の高値を「上値（レンジ上限）」とみなすか
    min_base_days=10,      # 上値をつけてから最低何日たっているか（レンジ形成の確認）
    near_pct=5.0,          # ブレイク待ち：上値まで何%以内か
    vol_dry=0.85,          # ブレイク待ち：直近10日の出来高が、その前100日平均の何倍未満か（売り枯れ）
    min_adr=2.5,           # 1日の平均値動き（%）の下限
    breakout_vol=1.5,      # 当日ブレイク：出来高が50日平均の何倍以上か
    streak_min=3,          # 連騰：何日以上の続伸か
    streak_max_gain=5.0,   # 連騰：1日の上昇率の上限（%）。急騰・S高は除外
    gap_max=3.0,           # 連騰：窓開けの上限（%）
)
# ======================================================


def log(msg):
    print(msg, flush=True)


def load_universe():
    """JPXの上場銘柄一覧（data_j.xls）から内国株のコードと銘柄名を取得"""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "data_j.xls")
    fresh = os.path.exists(path) and (time.time() - os.path.getmtime(path) < 7 * 86400)
    if not fresh:
        log("銘柄一覧をJPXから取得中...")
        try:
            req = urllib.request.Request(JPX_URL, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as r, open(path, "wb") as f:
                f.write(r.read())
        except Exception as e:
            if not os.path.exists(path):
                log(f"銘柄一覧の取得に失敗しました: {e}")
                log(f"JPXのサイトから data_j.xls を手動で保存し、{path} に置いてください。")
                sys.exit(1)
            log("取得に失敗したため、前回の一覧を使います。")
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
                                 auto_adjust=True, threads=True, progress=False)
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
    ma5, ma25, ma50 = (s.rolling(n).mean().values for n in (5, 25, 50))
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

    streak, gains, gaps = 0, [], []
    for i in range(len(c) - 1, 0, -1):
        if c[i] > c[i - 1]:
            streak += 1
            gains.append((c[i] / c[i - 1] - 1) * 100)
            gaps.append((o[i] / c[i - 1] - 1) * 100)
        else:
            break

    return dict(
        close=close, chg=(c[-1] / c[-2] - 1) * 100, turnover20=turnover20, adr=adr,
        ma5=ma5[-1], ma25=ma25[-1], ma50=ma50[-1],
        ma25_up=ma25[-1] > ma25[-6], ma50_up=ma50[-1] >= ma50[-11] * 0.99,   # 50日線が下向きでない
        dev25=(close / ma25[-1] - 1) * 100,
        vol_today=v[-1] / vol50, vol10r=vol10 / vol_prev if vol_prev > 0 else 1.0, vol5r=vol5 / vol50,
        res=res, res_age=res_age, dist=(res / close - 1) * 100, low10=low10,
        streak=streak, max_gain=max(gains) if gains else 0, max_gap=max(gaps) if gaps else 0,
        spark=[round(x, 2) for x in c[-60:]],
        date=str(d.index[-1].date()),
    )


def screen(universe, prices):
    rows = []
    for _, u in universe.iterrows():
        d = prices.get(u.code)
        if d is None:
            continue
        a = analyze(d)
        if a is None or a["close"] < CFG["min_price"] or a["turnover20"] < CFG["min_turnover"]:
            continue
        a.update(code=u.code, name=u["name"], market=u.market, sector=u.sector)
        rows.append(a)
    df = pd.DataFrame(rows)
    if df.empty:
        return df, df, df, df

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
    B = B.sort_values("vol_today", ascending=False)

    # C. 静かな連騰
    C = df[(df.streak >= CFG["streak_min"]) & (df.max_gain <= CFG["streak_max_gain"])
           & (df.max_gap <= CFG["gap_max"]) & (df.ma5 > df.ma25) & (df.ma25 > df.ma50)
           & df.ma25_up & (df.vol5r >= 1.0) & (df.vol5r <= 3.0)].copy()
    C = C.sort_values(["streak", "turnover20"], ascending=[False, False])
    return df, A, B, C


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


def write_html(df, A, B, C):
    os.makedirs(OUT, exist_ok=True)
    date = df.date.max() if not df.empty else dt.date.today().isoformat()
    base = [
        ("コード", lambda r: f"<b>{r.code}</b>", "code"),
        ("銘柄", lambda r: f'{html.escape(r["name"])}<div class="sub">{html.escape(r.market)}・{html.escape(r.sector)}</div>', "name"),
        ("終値", lambda r: fmt(r.close, 1), "close"),
        ("前日比", lambda r: f'<span class="{"up" if r.chg >= 0 else "dn"}">{fmt(r.chg, 2, "%")}</span>', "chg"),
    ]
    tail = [
        ("売買代金20日", lambda r: fmt(r.turnover20 / 1e8, 1, "億"), "turnover20"),
        ("60日", lambda r: spark_svg(r.spark, r.res), None),
        ("チャート", lambda r: links(r.code), None),
    ]
    colsA = base + [
        ("上値(アラート)", lambda r: f"<b>{fmt(r.res, 1)}</b>", "res"),
        ("上値まで", lambda r: fmt(r.dist, 1, "%"), "dist"),
        ("上値から日数", lambda r: f"{int(r.res_age)}日", "res_age"),
        ("出来高(10日/前100日)", lambda r: fmt(r.vol10r, 2, "倍"), "vol10r"),
        ("平均値動き", lambda r: fmt(r.adr, 1, "%"), "adr"),
        ("損切り目安(10日安値)", lambda r: f'{fmt(r.low10, 1)}<div class="sub">-{fmt(r.risk, 1, "%")}</div>', "risk"),
        ("値動き/損切り幅", lambda r: fmt(r.rr, 1, "R"), "rr"),
    ] + tail
    colsB = base + [
        ("抜けた上値", lambda r: fmt(r.res, 1), "res"),
        ("上値からの上昇", lambda r: fmt(r.ext, 1, "%"), "ext"),
        ("出来高(当日/50日)", lambda r: fmt(r.vol_today, 1, "倍"), "vol_today"),
        ("平均値動き", lambda r: fmt(r.adr, 1, "%"), "adr"),
        ("損切り目安(10日安値)", lambda r: f'{fmt(r.low10, 1)}<div class="sub">-{fmt(r.risk, 1, "%")}</div>', "risk"),
    ] + tail
    colsC = base + [
        ("連騰", lambda r: f"<b>{int(r.streak)}日</b>", "streak"),
        ("最大上昇/日", lambda r: fmt(r.max_gain, 1, "%"), "max_gain"),
        ("25日線乖離", lambda r: fmt(r.dev25, 1, "%"), "dev25"),
        ("出来高(5日/50日)", lambda r: fmt(r.vol5r, 2, "倍"), "vol5r"),
        ("上値まで", lambda r: fmt(r.dist, 1, "%") if r.dist >= 0 else "高値更新中", "dist"),
    ] + tail

    cfg_txt = (f"売買代金{CFG['min_turnover']/1e8:.0f}億円以上・株価{CFG['min_price']}円以上／"
               f"上値＝過去{CFG['range_days']}日高値（{CFG['min_base_days']}日以上前）")
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
td:nth-child(2),th:nth-child(2){{text-align:left}}
.sub{{font-size:11px;color:var(--mut)}} .up{{color:var(--up)}} .dn{{color:var(--dn)}}
a{{color:var(--acc)}} .empty{{color:var(--mut)}}
</style></head><body><main>
<h1>東証スクリーナー　{date}</h1>
<p class="note">対象 {len(df):,}銘柄（{html.escape(cfg_txt)}）。見出しをクリックで並べ替え。</p>
<div class="tabs"><a href="#a">A ブレイク待ち {len(A)}</a><a href="#b">B 当日ブレイク {len(B)}</a><a href="#c">C 静かな連騰 {len(C)}</a></div>

<h2 id="a">A. ブレイク待ち（アラート候補） {len(A)}銘柄</h2>
<p class="note">上値の{CFG['near_pct']:.0f}%以内・出来高が細っている・50日線の上。上値の価格にアラートを置く候補。赤の点線が上値。</p>
{table(A, colsA)}

<h2 id="b">B. 当日ブレイク {len(B)}銘柄</h2>
<p class="note">終値で上値を超え、出来高が50日平均の{CFG['breakout_vol']}倍以上。「上値からの上昇」が大きいほど、今から買うと損切り幅が広がる。</p>
{table(B, colsB)}

<h2 id="c">C. 静かな連騰 {len(C)}銘柄</h2>
<p class="note">{CFG['streak_min']}日以上続伸・1日+{CFG['streak_max_gain']:.0f}%以内・窓開け{CFG['gap_max']:.0f}%以内・5日＞25日＞50日線・出来高は平常の1〜3倍。</p>
{table(C, colsC)}
</main>
<script>
function sortT(th){{const t=th.closest('table'),i=[...th.parentNode.children].indexOf(th),b=t.tBodies[0];
const asc=th.dataset.asc!=='1';th.dataset.asc=asc?'1':'0';
const rows=[...b.rows].sort((x,y)=>{{let a=x.cells[i].dataset.v,c=y.cells[i].dataset.v;const na=parseFloat(a),nc=parseFloat(c);
if(!isNaN(na)&&!isNaN(nc))return asc?na-nc:nc-na;return asc?String(a).localeCompare(c,'ja'):String(c).localeCompare(a,'ja');}});
rows.forEach(r=>b.appendChild(r));}}
</script></body></html>"""
    path = os.path.join(OUT, f"screen_{date}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    with open(os.path.join(OUT, "latest.html"), "w", encoding="utf-8") as f:
        f.write(page)
    drop = ["spark"]
    for name, x in (("A_breakout_watch", A), ("B_breakout_today", B), ("C_quiet_streak", C)):
        x.drop(columns=drop, errors="ignore").to_csv(os.path.join(OUT, f"{name}_{date}.csv"),
                                                     index=False, encoding="utf-8-sig")
    return path


def main():
    t0 = time.time()
    uni = load_universe()
    log(f"対象銘柄: {len(uni):,}")
    prices = fetch_prices(uni.code.tolist())
    log(f"株価データ取得: {len(prices):,}銘柄")
    df, A, B, C = screen(uni, prices)
    if df.empty:
        log("分析できる銘柄がありませんでした。ネット接続を確認してください。")
        return
    path = write_html(df, A, B, C)
    log(f"\n完了（{time.time() - t0:.0f}秒）  A:{len(A)}  B:{len(B)}  C:{len(C)}")
    log(f"結果: {path}")
    if "--no-open" not in sys.argv:
        webbrowser.open("file:///" + path.replace("\\", "/"))


if __name__ == "__main__":
    main()
