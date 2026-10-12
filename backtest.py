# -*- coding: utf-8 -*-
"""
D. 押し目からの3連陽（25日線型・75日線型）の検証。
過去約1年の全銘柄・全営業日でシグナルを計算し、
・正解例（MRO・富士フイルム・九州FG）が拾えるか
・1日に何件出るか
・出たあと10日・20日でどう動いたか
を docs/backtest/index.html と CSV に出す。本番のスクリーナーには影響しない。
"""
import os, sys, time, html, pickle, datetime as dt

import numpy as np
import pandas as pd
import yfinance as yf

import screener as S

OUT = os.path.join(S.BASE, "docs", "backtest")

# ===== 検証する条件（ここを書き換えて再実行） =====
P = dict(
    touch_win=5,        # 1本目の陽線と、その前何日以内に線へタッチしていればよいか
    touch_above=1.0,    # 安値が線の何%上までなら「タッチ」とみなすか
    max_under_adr=1.5,  # 線の下への割り込みは、平均値動き(%)の何倍まで許すか（深いものは崩れとして除外）
    pre25=(25, 8, 0.7), # 25型：タッチ前の期間（何日前〜何日前）に、終値が25日線の上にあった割合の下限
    pre75=(40, 8, 0.8), # 75型：同じく75日線の上
    ext_adr=3.0,        # 3日上昇が平均値動きの何倍を超えたら「走りすぎ」印
    alert_days=5,       # アラート（3本目の高値）に何日以内に届けば「買えた」とするか
)
# 正解例（コード, 名前, 探す期間）
CASES = [
    ("3064", "MRO", "2026-09-24", "2026-10-09"),
    ("4901", "富士フイルム", "2026-06-15", "2026-07-10"),
    ("7180", "九州FG", "2026-08-15", "2026-09-15"),
]
# ================================================


def fetch(codes):
    os.makedirs(S.CACHE, exist_ok=True)
    cpath = os.path.join(S.CACHE, f"bt_prices_{dt.date.today()}.pkl")
    if os.path.exists(cpath):
        with open(cpath, "rb") as f:
            return pickle.load(f)
    data, tickers = {}, [f"{c}.T" for c in codes]
    for i in range(0, len(tickers), 100):
        part = tickers[i:i + 100]
        S.log(f"株価取得中 {i + len(part)}/{len(tickers)}")
        for _ in range(3):
            try:
                df = yf.download(part, period="2y", interval="1d", group_by="ticker",
                                 auto_adjust=False, threads=True, progress=False)
                break
            except Exception as e:
                S.log(f"  再試行 ({e})")
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


def calc(d):
    """1銘柄の全日について、Dの判定に使う値を配列で返す"""
    o, h, l, c, v = (d[k].astype(float).values for k in ["Open", "High", "Low", "Close", "Volume"])
    s = pd.Series(c)
    ma25 = s.rolling(25).mean().values
    ma75 = s.rolling(75).mean().values
    adr = pd.Series((h / l - 1) * 100).rolling(20).mean().values
    turn = pd.Series(c * v).rolling(20).mean().values
    vol50 = pd.Series(v).rolling(50).mean().shift(1).values
    return dict(o=o, h=h, l=l, c=c, v=v, ma25=ma25, ma75=ma75, adr=adr, turn=turn, vol50=vol50,
                dates=[x.date() for x in d.index])


def judge(x, t):
    """t日（3本目の陽線の日）にDが成立するか。条件ごとの結果も返す"""
    o, h, l, c = x["o"], x["h"], x["l"], x["c"]
    ma25, ma75, adr = x["ma25"], x["ma75"], x["adr"]
    r = dict(ok=False)
    if t < 120 or np.isnan(ma75[t - 50]):
        r["fail"] = "データ不足"
        return r
    bull = lambda i: c[i] > o[i]
    r["3陽"] = bull(t) and bull(t - 1) and bull(t - 2)
    r["安値切上"] = l[t] > l[t - 1] > l[t - 2]
    r["初成立"] = not (bull(t - 3) and l[t - 2] > l[t - 3])     # 4本目以降の重複を除く
    r["流動性"] = x["turn"][t] >= S.CFG["min_turnover"] and c[t] >= S.CFG["min_price"]
    r["25>75"] = ma25[t] > ma75[t]
    r["75上向"] = ma75[t] >= ma75[t - 10] * 0.995

    # タッチの判定（1本目の陽線 t-2 と、その前 touch_win 日）
    w0, w1 = t - 2 - P["touch_win"], t - 2
    a = adr[t]
    best = None
    for line, name in ((ma75, "75"), (ma25, "25")):
        for i in range(w0, w1 + 1):
            up = l[i] <= line[i] * (1 + P["touch_above"] / 100)
            deep = l[i] >= line[i] * (1 - a * P["max_under_adr"] / 100)
            if up and deep:
                best = (name, i)
                break
        if best:
            break
    r["タッチ"] = best[0] if best else None
    if not best:
        # 割り込みが深すぎたか、届いていないか
        deepest = min(l[w0:w1 + 1] / ma25[w0:w1 + 1]) - 1
        r["fail"] = "線に届かず" if deepest > P["touch_above"] / 100 else "割り込み深すぎ"
    else:
        name, ti = best
        line = ma75 if name == "75" else ma25
        b, e, ratio = P["pre75"] if name == "75" else P["pre25"]
        above = np.mean(c[t - b:t - e] > line[t - b:t - e])
        r["事前に線の上"] = above >= ratio
        r["25上向"] = name == "75" or ma25[t] >= ma25[t - 5] * 0.995
        r["touch_i"] = ti
        r["above_ratio"] = above

    keys = ["3陽", "安値切上", "初成立", "流動性", "25>75", "75上向", "タッチ", "事前に線の上", "25上向"]
    fails = [k for k in keys if not r.get(k)]
    if not fails:
        r["ok"] = True
    elif "fail" not in r:
        r["fail"] = "・".join(fails)
    elif r.get("タッチ") is None:
        r["fail"] = "・".join([k for k in keys[:6] if not r.get(k)] + [r["fail"]])
    return r


def outcome(x, t, r):
    o, h, l, c, v = x["o"], x["h"], x["l"], x["c"], x["v"]
    ti = r["touch_i"]
    line = x["ma75"] if r["タッチ"] == "75" else x["ma25"]
    stop = float(min(l[ti:t + 1]))
    a = x["adr"][t]
    g3 = (c[t] / c[t - 3] - 1) * 100
    pre_v = np.mean(v[ti - 5:ti + 1]) / x["vol50"][t]
    now_v = np.mean(v[t - 2:t + 1]) / x["vol50"][t]
    hi_prev = float(np.max(h[t - 60:t - 2]))
    out = dict(
        type=r["タッチ"], close=c[t], line=line[t], dev=(c[t] / line[t] - 1) * 100,
        g3=g3, ext=g3 > a * P["ext_adr"], adr=a, stop=stop, risk=(c[t] - stop) / c[t] * 100,
        bars_since_touch=t - ti, gap2575=(x["ma25"][t] / x["ma75"][t] - 1) * 100,
        reclaim25=c[t] > x["ma25"][t], vol_dip=pre_v, vol_3=now_v, to_high=(hi_prev / c[t] - 1) * 100,
    )
    n = len(c)
    for k in (10, 20):
        out[f"ret{k}"] = (c[t + k] / c[t] - 1) * 100 if t + k < n else np.nan
    if t + 20 < n:
        out["maxup20"] = (max(h[t + 1:t + 21]) / c[t] - 1) * 100
        out["stop20"] = bool(min(l[t + 1:t + 21]) < stop)
    else:
        out["maxup20"], out["stop20"] = np.nan, None
    # 今の手法：3本目の高値にアラート→届いたら買う
    alert = h[t]
    hit = [i for i in range(t + 1, min(t + 1 + P["alert_days"], n)) if h[i] > alert]
    out["alert"] = alert
    out["alert_hit"] = bool(hit) if t + P["alert_days"] < n else None
    if hit:
        i = hit[0]
        entry = max(alert, o[i])                 # 窓を開けて寄った場合は始値で買い
        out["a_entry"] = entry
        out["a_risk"] = (entry - stop) / entry * 100
        if i + 20 < n:
            out["a_ret20"] = (c[i + 20] / entry - 1) * 100
            lows = l[i:i + 21]
            out["a_stop20"] = bool(min(lows) < stop)
    return out


def candles_svg(x, t, ti=None, line_name=None, before=45, after=15):
    """シグナル前後のローソク足（日本式：陽線=赤・陰線=緑）と25日線・75日線"""
    a, b = max(0, t - before), min(len(x["c"]), t + after + 1)
    o, h, l, c = (x[k][a:b] for k in "ohlc")
    m25, m75 = x["ma25"][a:b], x["ma75"][a:b]
    lo = np.nanmin(np.concatenate([l, m25, m75]))
    hi = np.nanmax(np.concatenate([h, m25, m75]))
    W, H, n = 300, 110, b - a
    bw = W / n
    y = lambda p: H - (p - lo) / ((hi - lo) or 1) * H
    parts = []
    for i in range(n):
        cx = i * bw + bw / 2
        up = c[i] >= o[i]
        col = "#e5484d" if up else "#30a46c"
        parts.append(f'<line x1="{cx:.1f}" x2="{cx:.1f}" y1="{y(h[i]):.1f}" y2="{y(l[i]):.1f}" stroke="{col}" stroke-width="1"/>')
        y0, y1 = sorted((y(o[i]), y(c[i])))
        parts.append(f'<rect x="{cx - bw * 0.35:.1f}" y="{y0:.1f}" width="{bw * 0.7:.1f}" height="{max(y1 - y0, 0.8):.1f}" fill="{col}"/>')
    for m, col in ((m25, "#d4a72c"), (m75, "#3e9bd6")):
        pts = " ".join(f"{i * bw + bw / 2:.1f},{y(p):.1f}" for i, p in enumerate(m) if not np.isnan(p))
        parts.append(f'<polyline fill="none" stroke="{col}" stroke-width="1.2" points="{pts}"/>')
    sx = (t - a) * bw + bw / 2
    parts.insert(0, f'<rect x="{sx - bw * 2.5:.1f}" y="0" width="{bw * 3:.1f}" height="{H}" fill="rgba(120,120,255,.13)"/>')
    if ti is not None and ti >= a:
        tx = (ti - a) * bw + bw / 2
        parts.append(f'<circle cx="{tx:.1f}" cy="{y(x["l"][ti]) + 5:.1f}" r="3" fill="none" stroke="#888"/>')
    return f'<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" style="background:#0e0f14;border-radius:6px">{"".join(parts)}</svg>'


def main():
    t0 = time.time()
    uni = S.load_universe()
    names = dict(zip(uni.code, uni["name"]))
    prices = fetch(uni.code.tolist())
    S.log(f"株価データ: {len(prices):,}銘柄")

    sigs, base, X = [], [], {}
    for code, d in prices.items():
        if len(d) < 140:
            continue
        x = calc(d)
        X[code] = x
        n = len(x["c"])
        start = max(120, n - 250)                 # 直近約1年
        o_, c_ = x["o"], x["c"]
        for t in range(start, n):
            # 比較用：同じ流動性条件の全銘柄・全日の20日後リターン
            if t + 20 < n and x["turn"][t] >= S.CFG["min_turnover"] and x["c"][t] >= S.CFG["min_price"]:
                base.append((x["c"][t + 20] / x["c"][t] - 1) * 100)
            if not (c_[t] > o_[t] and c_[t - 1] > o_[t - 1] and c_[t - 2] > o_[t - 2]):
                continue
            r = judge(x, t)
            if r["ok"]:
                rec = dict(date=x["dates"][t], code=code, name=names.get(code, ""), t=t, ti=r["touch_i"])
                rec.update(outcome(x, t, r))
                sigs.append(rec)
    df = pd.DataFrame(sigs)
    S.log(f"シグナル {len(df):,}件（{time.time() - t0:.0f}秒）")

    # 正解例の診断
    case_rows = []
    for code, nm, a, b in CASES:
        x = X.get(code)
        if x is None:
            case_rows.append(dict(code=code, name=nm, date="-", res="株価データなし"))
            continue
        found = False
        for t, day in enumerate(x["dates"]):
            if not (dt.date.fromisoformat(a) <= day <= dt.date.fromisoformat(b)):
                continue
            r = judge(x, t)
            if r.get("3陽") or r["ok"]:
                found = True
                case_rows.append(dict(code=code, name=nm, date=day, t=t, ok=r["ok"],
                                      res="成立（" + r["タッチ"] + "型）" if r["ok"] else "不成立：" + r.get("fail", ""),
                                      detail=r))
        if not found:
            case_rows.append(dict(code=code, name=nm, date=f"{a}〜{b}", res="期間内に陽線3本の並びなし"))
    write(df, base, case_rows, X)
    S.log(f"完了（{time.time() - t0:.0f}秒）")


def pct(x, nd=1):
    return "-" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:+.{nd}f}%"


def stats(g, base_mean):
    n = len(g)
    if n == 0:
        return None
    r20 = g.ret20.dropna()
    st = g.stop20.dropna().astype(bool)
    ah = g.alert_hit.dropna().astype(bool)
    ar = g.a_ret20.dropna() if "a_ret20" in g else pd.Series(dtype=float)
    return dict(
        n=n, r10=g.ret10.mean(), r20=r20.mean(), med20=r20.median(),
        win20=(r20 > 0).mean() * 100 if len(r20) else np.nan, edge=r20.mean() - base_mean,
        up20=g.maxup20.mean(), stop=st.mean() * 100 if len(st) else np.nan, risk=g.risk.median(),
        ahit=ah.mean() * 100 if len(ah) else np.nan, ar20=ar.mean() if len(ar) else np.nan,
    )


def write(df, base, cases, X):
    os.makedirs(OUT, exist_ok=True)
    E = html.escape
    base_mean = float(np.mean(base)) if base else 0.0
    if not df.empty:
        df.drop(columns=["t", "ti"]).to_csv(os.path.join(OUT, "signals.csv"), index=False, encoding="utf-8-sig")

    # 集計
    groups = [("全体", df)]
    if not df.empty:
        groups += [("25型", df[df.type == "25"]), ("75型", df[df.type == "75"]),
                   ("走りすぎ印あり", df[df.ext]), ("走りすぎ印なし", df[~df.ext]),
                   ("線の束 3%以内", df[df.gap2575.abs() <= 3]), ("線の束 3%超", df[df.gap2575.abs() > 3]),
                   ("75型・25日線奪回済", df[(df.type == "75") & df.reclaim25]),
                   ("75型・25日線未奪回", df[(df.type == "75") & ~df.reclaim25]),
                   ("押し目で出来高が細った（0.8倍未満）", df[df.vol_dip < 0.8]),
                   ("3連陽で出来高増（1.2倍以上）", df[df.vol_3 >= 1.2])]
    srows = []
    for nm, g in groups:
        s = stats(g, base_mean)
        if not s:
            srows.append(f"<tr><td>{E(nm)}</td><td>0</td>" + "<td>-</td>" * 9 + "</tr>")
            continue
        srows.append(
            f"<tr><td>{E(nm)}</td><td>{s['n']:,}</td><td>{pct(s['r10'])}</td><td>{pct(s['r20'])}</td>"
            f"<td>{pct(s['med20'])}</td><td>{s['win20']:.0f}%</td><td><b>{pct(s['edge'])}</b></td>"
            f"<td>{pct(s['up20'])}</td><td>{s['stop']:.0f}%</td><td>{s['risk']:.1f}%</td>"
            f"<td>{s['ahit']:.0f}% / {pct(s['ar20'])}</td></tr>")

    # 1日あたりの件数
    if not df.empty:
        per = df.groupby("date").size()
        all_days = sorted({d for x in X.values() for d in x["dates"][-250:]})
        per = per.reindex(all_days, fill_value=0)
        cnt_txt = (f"1日平均 {per.mean():.1f}件（中央値 {per.median():.0f}件・最多 {per.max()}件 {per.idxmax()}）。"
                   f"0件の日 {int((per == 0).sum())}日／{len(per)}日。")
        top = per.sort_values(ascending=False).head(5)
        cnt_txt += " 多かった日: " + "、".join(f"{d}（{v}件）" for d, v in top.items())
        mx = max(per.max(), 1)
        bars = "".join(
            f'<div title="{d} {v}件" style="flex:1;background:#5b7fd6;height:{v / mx * 100:.0f}%;min-width:1px"></div>'
            for d, v in per.items())
        bar_html = (f'<div style="display:flex;align-items:flex-end;gap:1px;height:80px;border-bottom:1px solid var(--line)">{bars}</div>'
                    f'<div class="sub" style="display:flex;justify-content:space-between"><span>{per.index[0]}</span><span>{per.index[-1]}</span></div>')
    else:
        cnt_txt, bar_html = "シグナルなし", ""

    # 正解例
    crow = []
    for r in cases:
        x = X.get(r["code"])
        det = r.get("detail") or {}
        svg = candles_svg(x, r["t"], det.get("touch_i")) if x is not None and "t" in r else ""
        cls = "ok" if r.get("ok") else "ng"
        extra = ""
        if det.get("touch_i") is not None:
            extra = f'<div class="sub">タッチ {x["dates"][det["touch_i"]]}・事前に線の上 {det.get("above_ratio", 0) * 100:.0f}%</div>'
        crow.append(f'<tr><td>{r["code"]} {E(r["name"])}</td><td>{r["date"]}</td>'
                    f'<td class="{cls}">{E(r["res"])}{extra}</td><td>{svg}</td></tr>')

    # 直近のシグナル（目視確認用）
    recent = ""
    if not df.empty:
        rows = []
        for _, r in df.sort_values("date", ascending=False).head(80).iterrows():
            x = X[r.code]
            rows.append(
                f'<tr><td><a href="{S.tv(r.code)}" target="_blank">{r.code}</a> {E(r["name"])}</td><td>{r.date}</td>'
                f'<td>{r["type"]}型{"・奪回" if r["type"] == "75" and r.reclaim25 else ""}</td><td>{pct(r.g3)}{" ⚠" if r.ext else ""}</td>'
                f'<td>{r.risk:.1f}%</td><td>{pct(r.gap2575)}</td><td>{pct(r.ret10)}</td><td>{pct(r.ret20)}</td>'
                f'<td>{candles_svg(x, int(r.t), int(r.ti))}</td></tr>')
        recent = "".join(rows)

    p = ", ".join(f"{k}={v}" for k, v in P.items())
    page = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>D 検証</title>
<style>
:root{{--bg:#fafaf9;--fg:#1c1c1a;--mut:#6b6b66;--line:#e4e4e0;--card:#fff}}
@media (prefers-color-scheme:dark){{:root{{--bg:#141413;--fg:#ececea;--mut:#9a9a94;--line:#2c2c2a;--card:#1c1c1b}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 -apple-system,"Hiragino Sans","Yu Gothic UI",sans-serif}}
main{{max-width:1200px;margin:0 auto;padding:20px 16px 60px}} h1{{font-size:21px}} h2{{font-size:17px;margin-top:34px}}
.wrap{{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--card)}}
table{{border-collapse:collapse;width:100%;white-space:nowrap}} th,td{{padding:6px 9px;border-bottom:1px solid var(--line);text-align:right;vertical-align:middle}}
th{{font-size:12px;color:var(--mut)}} td:first-child,th:first-child{{text-align:left}}
.sub,.note{{color:var(--mut);font-size:12px}} .ok{{color:#2f9e5b;font-weight:600;text-align:left}} .ng{{color:#d97706;text-align:left;white-space:normal}}
a{{color:#5b7fd6}}
</style></head><body><main>
<h1>D. 押し目からの3連陽　検証（直近約1年）</h1>
<p class="note">作成 {dt.datetime.now():%Y-%m-%d %H:%M}（UTC）／条件: {E(p)}<br>
比較の基準：同じ流動性条件の全銘柄・全日の20日後リターン平均 {base_mean:+.2f}%（{len(base):,}件）。「基準との差」がプラスなら、全体より良かったことになります。</p>

<h2>1. 正解例が拾えたか</h2>
<p class="note">各期間で「陽線3本」がそろった日を表示。不成立の場合は、どの条件で落ちたか。チャートの青い帯が3本の陽線、丸がタッチした日。黄＝25日線、青＝75日線。</p>
<div class="wrap"><table><tr><th>銘柄</th><th>日付</th><th>判定</th><th>チャート</th></tr>{"".join(crow)}</table></div>

<h2>2. 1日に何件出るか</h2>
<p>{E(cnt_txt)}</p>{bar_html}

<h2>3. 出たあとの動き</h2>
<p class="note">終値＝3本目の終値から測った値。「損切り」＝20日以内に損切り目安（タッチ〜3本目の最安値）を下回った割合。「アラート」＝翌5日以内に3本目の高値を超えた割合／そこで買った場合の20日後。</p>
<div class="wrap"><table><tr><th>区分</th><th>件数</th><th>10日後</th><th>20日後 平均</th><th>20日後 中央値</th><th>上昇した割合</th><th>基準との差</th><th>20日内の最大上昇</th><th>損切り</th><th>損切り幅</th><th>アラート到達/20日後</th></tr>{"".join(srows)}</table></div>

<h2>4. 直近のシグナル（形の確認用・新しい順80件）</h2>
<p class="note">「⚠」は走りすぎ印。「線の束」は25日線と75日線の差。</p>
<div class="wrap"><table><tr><th>銘柄</th><th>3本目</th><th>型</th><th>3日上昇</th><th>損切り幅</th><th>線の束</th><th>10日後</th><th>20日後</th><th>チャート</th></tr>{recent}</table></div>
<p class="note">全シグナルの一覧: <a href="signals.csv">signals.csv</a></p>
</main></body></html>"""
    with open(os.path.join(OUT, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)


if __name__ == "__main__":
    main()
