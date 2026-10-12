# -*- coding: utf-8 -*-
"""
D. 押し目からの3連陽 の判定（本番の screener.py と検証の backtest.py で共通）
・25日線／50日線／75日線のいずれかに安値が触れ（少しの割り込みは可）
・そこから陽線3本・安値切り上げ
・上昇トレンド中（25日線＞75日線、75日線が上向き〜横ばい）で、調整前は触れた線の上にいた
"""
import numpy as np
import pandas as pd

# ===== 基本条件 =====
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


def calc(d):
    o, h, l, c, v = (d[k].astype(float).values for k in ["Open", "High", "Low", "Close", "Volume"])
    s = pd.Series(c)
    return dict(o=o, h=h, l=l, c=c, v=v,
                ma25=s.rolling(25).mean().values, ma50=s.rolling(50).mean().values, ma75=s.rolling(75).mean().values,
                adr=pd.Series((h / l - 1) * 100).rolling(20).mean().values,
                turn=pd.Series(c * v).rolling(20).mean().values,
                vol50=pd.Series(v).rolling(50).mean().shift(1).values,
                dates=[x.date() for x in d.index])


def liquid(x, t, cfg):
    return x["turn"][t] >= cfg["min_turnover"] and x["c"][t] >= cfg["min_price"]


def judge(x, t, cfg):
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
    r["流動性"] = liquid(x, t, cfg)
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
        type=r["タッチ"], close=c[t], line=line[t], alert=h[t], dev=(c[t] / line[t] - 1) * 100, g3=g3, ext=g3 > a * P["ext_adr"], adr=a,
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


# ===== 本番で使う追加条件（2026/10 の検証で決定） =====
# 売り枯れ（押し目の出来高0.8倍未満）は期間外の検証で効果が消えたため、条件ではなく印にとどめる
PROD = [
    ("50・75型は25日線を奪回済み", lambda d: (d["type"] == "25") | d["reclaim25"]),
    ("3連陽の出来高が平常の1.5倍未満", lambda d: d["vol_3"] < 1.5),
    ("60日騰落が市場中央値より上", lambda d: d["rs60"] > 0),
    ("1年高値まで20%以内", lambda d: d["room1y"] <= 20),
]
DRY = 0.8   # 押し目の出来高がこの倍率未満なら「売り枯れ」印


def prod_mask(d):
    m = pd.Series(True, index=d.index)
    for _, fn in PROD:
        m &= fn(d)
    return m
