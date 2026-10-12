# -*- coding: utf-8 -*-
"""
D. 押し目からの3連陽（25日線型・75日線型）の検証。
過去約1年の全銘柄・全営業日でシグナルを計算し、
・正解例（MRO・富士フイルム・九州FG）が拾えるか
・1日に何件出るか
・出たあと20日でどう動いたか（同じ日の全銘柄平均との差で評価）
・どの追加条件で優位性が上がるか（前半・後半に分けて確認）
を docs/backtest/ に出す。本番のスクリーナーには影響しない。
"""
import os, time, html, json, pickle, itertools, datetime as dt

import numpy as np
import pandas as pd
import yfinance as yf

import screener as S

OUT = os.path.join(S.BASE, "docs", "backtest")

# ===== 基本条件（ここを書き換えて再実行） =====
P = dict(
    touch_win=5,        # 1本目の陽線と、その前何日以内に線へタッチしていればよいか
    touch_above=1.0,    # 安値が線の何%上までなら「タッチ」とみなすか
    max_under_adr=1.5,  # 線の下への割り込みは、平均値動き(%)の何倍まで許すか（深いものは崩れとして除外）
    low_tol=0.3,        # 安値切り上げの許容（%）。前日安値をこの分だけ下回っても可（3本目は1本目より上が必須）
    pre25=(20, 5, 0.6), # 25型：タッチ前の期間（何日前〜何日前）に終値が線の上にあった割合の下限（今回の調整だけを見る）
    pre50=(30, 5, 0.8), # 50型：同じく50日線の上
    pre75=(40, 5, 0.8), # 75型：同じく75日線の上
    bundle=3.0,         # 25日線と75日線の差がこの%以内なら「束」。束のときは下側の線を基準に事前の位置を測る
    ext_adr=3.0,        # 3日上昇が平均値動きの何倍を超えたら「走りすぎ」印
    alert_days=5,       # アラート（3本目の高値）に何日以内に届けば「買えた」とするか
)
CASES = [
    ("3064", "MRO", "2026-09-24", "2026-10-09"),
    ("4901", "富士フイルム", "2026-06-15", "2026-07-10"),
    ("7180", "九州FG", "2026-08-15", "2026-09-15"),
]
# SBI証券アプリのチャートに表示されていた移動平均（2026/10/09）。期間の逆算に使う
CHART_MA = {"3064": (1979.6, 1943.1, 1923.7), "4901": (3327.1, 3382.5, 3441.6), "7180": (1660.5, 1592.2, 1565.7)}
CHART_DATE = dt.date(2026, 10, 9)
# ===============================================


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
    o, h, l, c, v = (d[k].astype(float).values for k in ["Open", "High", "Low", "Close", "Volume"])
    s = pd.Series(c)
    return dict(o=o, h=h, l=l, c=c, v=v,
                ma25=s.rolling(25).mean().values, ma50=s.rolling(50).mean().values, ma75=s.rolling(75).mean().values,
                adr=pd.Series((h / l - 1) * 100).rolling(20).mean().values,
                turn=pd.Series(c * v).rolling(20).mean().values,
                vol50=pd.Series(v).rolling(50).mean().shift(1).values,
                dates=[x.date() for x in d.index])


def liquid(x, t):
    return x["turn"][t] >= S.CFG["min_turnover"] and x["c"][t] >= S.CFG["min_price"]


def judge(x, t):
    """t日（3本目の陽線の日）に基本条件が成立するか。条件ごとの結果も返す"""
    o, l, c = x["o"], x["l"], x["c"]
    ma25, ma75, adr = x["ma25"], x["ma75"], x["adr"]
    r = dict(ok=False)
    if t < 120 or np.isnan(ma75[t - 50]):
        r["fail"] = "データ不足"
        return r
    bull = lambda i: c[i] > o[i]
    k = 1 - P["low_tol"] / 100
    rising = lambda i: l[i] >= l[i - 1] * k                       # i日の安値が前日から（ほぼ）切り上がっている
    r["3陽"] = bull(t) and bull(t - 1) and bull(t - 2)
    r["安値切上"] = rising(t) and rising(t - 1) and l[t] > l[t - 2]
    r["初成立"] = not (bull(t - 3) and rising(t - 2) and l[t - 1] > l[t - 3])
    r["流動性"] = liquid(x, t)
    r["25>75"] = ma25[t] > ma75[t]
    r["75上向"] = ma75[t] >= ma75[t - 10] * 0.995

    w0, w1 = t - 2 - P["touch_win"], t - 2
    a = adr[t]
    best = None
    for line, name in ((ma75, "75"), (x["ma50"], "50"), (ma25, "25")):
        for i in range(w0, w1 + 1):
            if line[i] * (1 - a * P["max_under_adr"] / 100) <= l[i] <= line[i] * (1 + P["touch_above"] / 100):
                best = (name, i)
                break
        if best:
            break
    r["タッチ"] = best[0] if best else None
    if not best:
        deepest = min(l[w0:w1 + 1] / ma25[w0:w1 + 1]) - 1
        r["fail"] = "線に届かず" if deepest > P["touch_above"] / 100 else "割り込み深すぎ"
    else:
        name, ti = best
        b, e, ratio = P["pre" + name]
        if name != "25":
            ref = ma75 if name == "75" else x["ma50"]
        else:
            bundled = abs(ma25[ti] / ma75[ti] - 1) * 100 <= P["bundle"]
            ref = np.minimum(ma25, ma75) * 0.99 if bundled else ma25
        above = float(np.mean(c[t - b:t - e] > ref[t - b:t - e]))
        r["事前に線の上"] = above >= ratio
        r["25上向"] = name != "25" or ma25[t] >= ma25[t - 5] * 0.995
        r["touch_i"], r["above_ratio"] = ti, above

    keys = ["3陽", "安値切上", "初成立", "流動性", "25>75", "75上向", "タッチ", "事前に線の上", "25上向"]
    fails = [k for k in keys if not r.get(k)]
    if not fails:
        r["ok"] = True
    elif r.get("タッチ") is None:
        r["fail"] = "・".join([k for k in keys[:6] if not r.get(k)] + [r["fail"]])
    else:
        r["fail"] = "・".join(fails)
    return r


def features(x, t, r):
    o, h, l, c, v = x["o"], x["h"], x["l"], x["c"], x["v"]
    ti = r["touch_i"]
    line = x["ma" + r["タッチ"]]
    stop = float(min(l[ti:t + 1]))
    a = x["adr"][t]
    g3 = (c[t] / c[t - 3] - 1) * 100
    hi_prev = float(np.max(h[t - 60:t - 2]))
    hi1y = float(np.max(h[max(0, t - 250):t]))
    out = dict(
        type=r["タッチ"], close=c[t], dev=(c[t] / line[t] - 1) * 100, g3=g3, ext=g3 > a * P["ext_adr"], adr=a,
        stop=stop, risk=(c[t] - stop) / c[t] * 100, gap2575=(x["ma25"][t] / x["ma75"][t] - 1) * 100,
        reclaim25=c[t] > x["ma25"][t],
        vol_dip=np.mean(v[ti - 5:ti + 1]) / x["vol50"][t],      # 押し目（タッチまでの6日）の出来高
        vol_3=np.mean(v[t - 2:t + 1]) / x["vol50"][t],          # 3連陽の出来高
        pb=(1 - min(l[t - 25:t + 1]) / hi_prev) * 100,          # 直近高値からの押しの深さ
        to_high=(hi_prev / c[t] - 1) * 100, room1y=(hi1y / c[t] - 1) * 100,
        ret60=(c[t] / c[t - 60] - 1) * 100,
        slope75=(x["ma75"][t] / x["ma75"][t - 20] - 1) * 100,
        turn=x["turn"][t] / 1e8,
    )
    n = len(c)
    out["ret20"] = (c[t + 20] / c[t] - 1) * 100 if t + 20 < n else np.nan
    out["ret10"] = (c[t + 10] / c[t] - 1) * 100 if t + 10 < n else np.nan
    out["stop20"] = bool(min(l[t + 1:t + 21]) < stop) if t + 20 < n else None
    hit = [i for i in range(t + 1, min(t + 1 + P["alert_days"], n)) if h[i] > h[t]]
    out["alert_hit"] = bool(hit) if t + P["alert_days"] < n else None
    if hit and hit[0] + 20 < n:
        i = hit[0]
        entry = max(h[t], o[i])
        out["a_ret20"] = (c[i + 20] / entry - 1) * 100
        out["a_risk"] = (entry - stop) / entry * 100
    return out


def candles_svg(x, t, ti=None, before=45, after=15):
    """シグナル前後のローソク足（陽線=赤・陰線=緑）と25日線（黄）・75日線（青）"""
    a, b = max(0, t - before), min(len(x["c"]), t + after + 1)
    o, h, l, c = (x[k][a:b] for k in "ohlc")
    m25, m75 = x["ma25"][a:b], x["ma75"][a:b]
    lo = np.nanmin(np.concatenate([l, m25, m75]))
    hi = np.nanmax(np.concatenate([h, m25, m75]))
    W, H, n = 300, 110, b - a
    bw = W / n
    y = lambda p: H - (p - lo) / ((hi - lo) or 1) * H
    parts = [f'<rect x="{(t - a) * bw - bw * 2:.1f}" y="0" width="{bw * 3:.1f}" height="{H}" fill="rgba(120,120,255,.15)"/>']
    for i in range(n):
        cx = i * bw + bw / 2
        col = "#e5484d" if c[i] >= o[i] else "#30a46c"
        parts.append(f'<line x1="{cx:.1f}" x2="{cx:.1f}" y1="{y(h[i]):.1f}" y2="{y(l[i]):.1f}" stroke="{col}"/>')
        y0, y1 = sorted((y(o[i]), y(c[i])))
        parts.append(f'<rect x="{cx - bw * 0.35:.1f}" y="{y0:.1f}" width="{bw * 0.7:.1f}" height="{max(y1 - y0, 0.8):.1f}" fill="{col}"/>')
    for m, col in ((m25, "#d4a72c"), (m75, "#3e9bd6")):
        pts = " ".join(f"{i * bw + bw / 2:.1f},{y(p):.1f}" for i, p in enumerate(m) if not np.isnan(p))
        parts.append(f'<polyline fill="none" stroke="{col}" stroke-width="1.2" points="{pts}"/>')
    if ti is not None and ti >= a:
        parts.append(f'<circle cx="{(ti - a) * bw + bw / 2:.1f}" cy="{y(x["l"][ti]) + 5:.1f}" r="3" fill="none" stroke="#aaa"/>')
    return f'<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" style="background:#0e0f14;border-radius:6px">{"".join(parts)}</svg>'


# ---------- 追加条件の候補（組み合わせて比べる） ----------
FILTERS = {
    "V": [("なし", lambda d: d.index == d.index), ("押し目出来高0.8倍未満", lambda d: d.vol_dip < 0.8),
          ("押し目出来高0.65倍未満", lambda d: d.vol_dip < 0.65)],
    "R": [("なし", None), ("50・75型は25日線奪回済のみ", lambda d: (d["type"] == "25") | d.reclaim25)],
    "Q": [("なし", None), ("3連陽の出来高1.5倍未満", lambda d: d.vol_3 < 1.5)],
    "S": [("なし", None), ("60日騰落が市場中央値より上", lambda d: d.rs60 > 0)],
    "H": [("なし", None), ("1年高値まで20%以内", lambda d: d.room1y <= 20)],
    "C": [("なし", None), ("一斉発生日（30件超）を除く", lambda d: d.day_n <= 30)],
}


# 2回目の検証で選んだ厳選条件（前半・後半とも超過プラス、1日2〜3件）
CHOSEN = [("押し目の出来高が平常の0.8倍未満", lambda d: d.vol_dip < 0.8),
          ("3連陽の出来高が平常の1.5倍未満", lambda d: d.vol_3 < 1.5),
          ("60日騰落が市場中央値より上", lambda d: d.rs60 > 0),
          ("1年高値まで20%以内", lambda d: d.room1y <= 20)]


def chosen_mask(d):
    m = pd.Series(True, index=d.index)
    for _, fn in CHOSEN:
        m &= fn(d)
    return m


def evaluate(df):
    days = df.date.nunique() and len(df.date.unique())
    mid = df.date.sort_values().iloc[len(df) // 2]
    ndays = df.attrs["ndays"]
    rows = []
    for combo in itertools.product(*[range(len(v)) for v in FILTERS.values()]):
        m = pd.Series(True, index=df.index)
        names = []
        for (key, opts), j in zip(FILTERS.items(), combo):
            nm, fn = opts[j]
            if fn is not None and j > 0:
                m &= fn(df)
                names.append(nm)
        g = df[m & df.ex20.notna()]
        if len(g) < 30:
            continue
        h1, h2 = g[g.date < mid], g[g.date >= mid]
        ar = g.a_ex20.dropna()
        rows.append(dict(
            combo="＋".join(names) or "基本条件のみ", k=len(names), n=len(g), per_day=len(df[m]) / ndays,
            ex20=g.ex20.mean(), med=g.ex20.median(), win=(g.ex20 > 0).mean() * 100,
            h1=h1.ex20.mean() if len(h1) else np.nan, h2=h2.ex20.mean() if len(h2) else np.nan,
            stop=g.stop20.dropna().astype(bool).mean() * 100, risk=g.risk.median(),
            a_ex20=ar.mean() if len(ar) else np.nan,
            t=g.ex20.mean() / (g.ex20.std(ddof=1) / np.sqrt(len(g))),
        ))
    return pd.DataFrame(rows).sort_values("t", ascending=False)


def ma_check(X):
    """チャートの移動平均の値から、何日線かを逆算する"""
    out = []
    for code, vals in CHART_MA.items():
        x = X.get(code)
        if x is None or CHART_DATE not in x["dates"]:
            out.append((code, "データなし"))
            continue
        t = x["dates"].index(CHART_DATE)
        c = x["c"]
        res = []
        for v in vals:
            cand = [(abs(c[t - n + 1:t + 1].mean() - v), n) for n in range(5, 201)]
            err, n = min(cand)
            res.append(f"{v:,.1f}→{n}日線（誤差{err:.1f}）")
        out.append((code, "／".join(res)))
    return out


def main():
    t0 = time.time()
    uni = S.load_universe()
    names = dict(zip(uni.code, uni["name"]))
    prices = fetch(uni.code.tolist())
    S.log(f"株価データ: {len(prices):,}銘柄")

    sigs, base, X = [], {}, {}
    for code, d in prices.items():
        if len(d) < 140:
            continue
        x = calc(d)
        X[code] = x
        n = len(x["c"])
        o_, c_ = x["o"], x["c"]
        for t in range(max(120, n - 250), n):
            if liquid(x, t):
                day = x["dates"][t]
                b = base.setdefault(day, [[], []])
                if t + 20 < n:
                    b[0].append((c_[t + 20] / c_[t] - 1) * 100)
                b[1].append((c_[t] / c_[t - 60] - 1) * 100)
            if not (c_[t] > o_[t] and c_[t - 1] > o_[t - 1] and c_[t - 2] > o_[t - 2]):
                continue
            r = judge(x, t)
            if r["ok"]:
                rec = dict(date=x["dates"][t], code=code, name=names.get(code, ""), t=t, ti=r["touch_i"])
                rec.update(features(x, t, r))
                sigs.append(rec)
    df = pd.DataFrame(sigs)
    # 同じ日の全銘柄（流動性条件内）の平均との差で評価する（地合いの影響を除く）
    day20 = {k: np.mean(v[0]) if v[0] else np.nan for k, v in base.items()}
    day60 = {k: np.median(v[1]) for k, v in base.items()}
    df["ex20"] = df.ret20 - df.date.map(day20)
    df["a_ex20"] = df.get("a_ret20", np.nan) - df.date.map(day20)
    df["rs60"] = df.ret60 - df.date.map(day60)
    df["day_n"] = df.date.map(df.groupby("date").size())
    df.attrs["ndays"] = len(base)
    S.log(f"シグナル {len(df):,}件（{time.time() - t0:.0f}秒）")

    grid = evaluate(df)
    case_rows = []
    for code, nm, a, b in CASES:
        x = X.get(code)
        found = False
        for t, day in enumerate(x["dates"] if x else []):
            if dt.date.fromisoformat(a) <= day <= dt.date.fromisoformat(b):
                r = judge(x, t)
                if r.get("3陽"):
                    found = True
                    f = features(x, t, r) if r["ok"] else {}
                    if f:
                        f["rs60"] = f["ret60"] - day60.get(day, np.nan)
                        fd = pd.DataFrame([f])
                        f["chosen_fail"] = [nm for nm, fn in CHOSEN if not bool(fn(fd).iloc[0])]
                    case_rows.append(dict(code=code, name=nm, date=day, t=t, ok=r["ok"], detail=r, f=f,
                                          res=("成立（" + r["タッチ"] + "型）") if r["ok"] else "不成立：" + r.get("fail", "")))
        if not found:
            case_rows.append(dict(code=code, name=nm, date=f"{a}〜{b}", res="期間内に陽線3本の並びなし"))
    write(df, grid, case_rows, X, ma_check(X))
    S.log(f"完了（{time.time() - t0:.0f}秒）")


def pct(x, nd=1):
    return "-" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:+.{nd}f}%"


def write(df, grid, cases, X, mac):
    os.makedirs(OUT, exist_ok=True)
    E = html.escape
    df.drop(columns=["t", "ti"]).to_csv(os.path.join(OUT, "signals.csv"), index=False, encoding="utf-8-sig")
    grid.to_csv(os.path.join(OUT, "grid.csv"), index=False, encoding="utf-8-sig")

    def grow(r):
        return (f"<tr><td style='white-space:normal;min-width:16em'>{E(r.combo)}</td><td>{r.n:,}</td><td>{r.per_day:.1f}</td>"
                f"<td><b>{pct(r.ex20)}</b></td><td>{pct(r.med)}</td><td>{r.win:.0f}%</td><td>{pct(r.h1)}</td><td>{pct(r.h2)}</td>"
                f"<td>{r.stop:.0f}%</td><td>{r.risk:.1f}%</td><td>{pct(r.a_ex20)}</td><td>{r.t:.1f}</td></tr>")
    single = grid[grid.k <= 1].sort_values("ex20", ascending=False)
    top = grid[(grid.h1 > 0) & (grid.h2 > 0) & (grid.n >= 100)].head(25)
    head = ("<tr><th>条件</th><th>件数</th><th>1日あたり</th><th>20日後 超過</th><th>超過 中央値</th><th>勝率</th>"
            "<th>前半</th><th>後半</th><th>損切り到達</th><th>損切り幅</th><th>アラート買い 超過</th><th>確からしさ</th></tr>")

    crow = []
    for r in cases:
        x = X.get(r["code"])
        det = r.get("detail") or {}
        svg = candles_svg(x, r["t"], det.get("touch_i")) if x is not None and "t" in r else ""
        f = r.get("f") or {}
        extra = ""
        if det.get("touch_i") is not None:
            extra = f'<div class="sub">タッチ {x["dates"][det["touch_i"]]}・事前に線の上 {det.get("above_ratio", 0) * 100:.0f}%</div>'
        if f:
            extra += (f'<div class="sub">押し目出来高 {f["vol_dip"]:.2f}倍・3連陽出来高 {f["vol_3"]:.2f}倍・'
                      f'60日の相対強さ {pct(f["rs60"], 0)}・1年高値まで {f["room1y"]:.0f}%・20日後 {pct(f["ret20"])}</div>')
            cf = f.get("chosen_fail", [])
            extra += (f'<div class="{"ok" if not cf else "ng"}">厳選条件: ' + ("通過" if not cf else "落ちる（" + "・".join(cf) + "）") + "</div>")
        crow.append(f'<tr><td>{r["code"]} {E(r["name"])}</td><td>{r["date"]}</td>'
                    f'<td class="{"ok" if r.get("ok") else "ng"}">{E(r["res"])}{extra}</td><td>{svg}</td></tr>')

    per = df.groupby("date").size().reindex(sorted({d for x in X.values() for d in x["dates"][-250:]}), fill_value=0)
    mx = max(per.max(), 1)
    bars = "".join(f'<div title="{d} {v}件" style="flex:1;background:#5b7fd6;height:{v / mx * 100:.0f}%;min-width:1px"></div>'
                   for d, v in per.items())

    rows = []
    ch = df[chosen_mask(df)]
    for _, r in ch.sort_values("date", ascending=False).head(60).iterrows():
        rows.append(
            f'<tr><td><a href="{S.tv(r.code)}" target="_blank">{r.code}</a> {E(r["name"])}</td><td>{r.date}</td>'
            f'<td>{r["type"]}型</td><td>{r.vol_dip:.2f}倍</td><td>{pct(r.rs60, 0)}</td><td>{r.room1y:.0f}%</td>'
            f'<td>{r.risk:.1f}%</td><td>{pct(r.ret20)}</td><td>{candles_svg(X[r.code], int(r.t), int(r.ti))}</td></tr>')

    def summ(g, nm):
        g2 = g[g.ex20.notna()]
        if len(g2) < 5:
            return f"<tr><td>{E(nm)}</td><td>{len(g)}</td>" + "<td>-</td>" * 7 + "</tr>"
        mid = df.date.sort_values().iloc[len(df) // 2]
        h1, h2 = g2[g2.date < mid].ex20.mean(), g2[g2.date >= mid].ex20.mean()
        return (f"<tr><td>{E(nm)}</td><td>{len(g):,}</td><td><b>{pct(g2.ex20.mean())}</b></td><td>{pct(g2.ret20.mean())}</td>"
                f"<td>{(g2.ex20 > 0).mean() * 100:.0f}%</td><td>{pct(h1)}</td><td>{pct(h2)}</td>"
                f"<td>{g2.stop20.dropna().astype(bool).mean() * 100:.0f}%</td><td>{pct(g2.a_ex20.mean())}</td></tr>")
    chs = [summ(ch, "厳選条件 全体")] + [summ(ch[ch["type"] == k], f"厳選条件 {k}型") for k in ("25", "50", "75")]
    chs += [summ(df, "基本条件のみ 全体")] + [summ(df[df["type"] == k], f"基本条件のみ {k}型") for k in ("25", "50", "75")]
    chper = ch.groupby("date").size().reindex(per.index, fill_value=0)
    ch_txt = (f"厳選条件: {len(ch):,}件・1日平均 {chper.mean():.1f}件・中央値 {chper.median():.0f}件・最多 {chper.max()}件・"
              f"0件の日 {int((chper == 0).sum())}日／{len(chper)}日")
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
<h1>D. 押し目からの3連陽　検証（直近約1年・2回目）</h1>
<p class="note">作成 {dt.datetime.now():%Y-%m-%d %H:%M}（UTC）／基本条件: {E(p)}<br>
「超過」＝3本目の終値から20日後の騰落率から、同じ日の全銘柄（売買代金・株価の条件内）の平均を引いた値。地合いの良し悪しを除いた実力です。
「前半・後半」は期間を半分に分けた超過。両方プラスなら、たまたまではない可能性が高くなります。「確からしさ」は平均÷誤差（2以上で偶然とは考えにくい）。</p>

<h2>1. 正解例が拾えたか</h2>
<div class="wrap"><table><tr><th>銘柄</th><th>3本目</th><th>判定</th><th>チャート</th></tr>{"".join(crow)}</table></div>
<p class="note">チャートの移動平均の期間（SBIアプリの値から逆算）: {"　".join(f"{c}: {E(t)}" for c, t in mac)}</p>

<h2>2. 基本条件だけの件数</h2>
<p>{len(df):,}件・1日平均 {len(df) / len(per):.1f}件（最多 {per.max()}件 {per.idxmax()}）</p>
<div style="display:flex;align-items:flex-end;gap:1px;height:80px;border-bottom:1px solid var(--line)">{bars}</div>

<h2>3. 厳選条件（{E("＋".join(n for n, _ in CHOSEN))}）</h2>
<p>{E(ch_txt)}</p>
<div class="wrap"><table><tr><th>区分</th><th>件数</th><th>20日後 超過</th><th>20日後 騰落</th><th>勝率</th><th>前半</th><th>後半</th><th>損切り到達</th><th>アラート買い 超過</th></tr>{"".join(chs)}</table></div>

<h2>4. 追加条件を1つだけ足した場合</h2>
<div class="wrap"><table>{head}{"".join(grow(r) for r in single.itertuples())}</table></div>

<h2>5. 組み合わせ（前半・後半とも超過プラス、100件以上、確からしさ順）</h2>
<div class="wrap"><table>{head}{"".join(grow(r) for r in top.itertuples())}</table></div>
<p class="note">全組み合わせ: <a href="grid.csv">grid.csv</a>／全シグナル: <a href="signals.csv">signals.csv</a></p>

<h2>6. 厳選条件のシグナル（新しい順60件）</h2>
<div class="wrap"><table><tr><th>銘柄</th><th>3本目</th><th>型</th><th>押し目出来高</th><th>60日の相対強さ</th><th>1年高値まで</th><th>損切り幅</th><th>20日後</th><th>チャート</th></tr>{"".join(rows)}</table></div>
</main></body></html>"""
    with open(os.path.join(OUT, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)


if __name__ == "__main__":
    main()
