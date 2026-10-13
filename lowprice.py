# -*- coding: utf-8 -*-
"""
低位株の除外ルールの検証（本番の抽出には影響しない）
再現（docs/track/replay.csv）の各シグナルに「その日の20日平均売買代金」と「TOPIX500かどうか」を付け、
除外ルールの候補ごとに「外れる銘柄」と「残る銘柄」の成績を比べる。

ねらい: 低位で売買代金が小さく、呼値が1円刻み（TOPIX500以外）のため値が飛ぶ銘柄を外したい。
        ただし LINEヤフーのような売買代金の大きい低位株は残したい。
注意: TOPIX500の判定は現在のJPX一覧の規模区分（当時の区分ではない）
結果は docs/track/lowprice.html と lowprice.csv
"""
import os, json, html, datetime as dt

import numpy as np
import pandas as pd

import screener as S
import replay as R
import track as T

OUT = os.path.join(S.BASE, "docs", "track")
OKU = 1e8

RULES = [  # 名前, 外す条件
    ("1,000円未満 かつ 売買代金5億円未満", lambda j: (j.close < 1000) & (j.to20 < 5 * OKU)),
    ("1,000円未満 かつ 売買代金10億円未満", lambda j: (j.close < 1000) & (j.to20 < 10 * OKU)),
    ("1,000円未満 かつ 売買代金20億円未満", lambda j: (j.close < 1000) & (j.to20 < 20 * OKU)),
    ("1,000円未満 かつ TOPIX500以外", lambda j: (j.close < 1000) & ~j.t500),
    ("1,000円未満 かつ TOPIX500以外 かつ 10億円未満", lambda j: (j.close < 1000) & ~j.t500 & (j.to20 < 10 * OKU)),
    ("参考: 1,000円未満すべて", lambda j: j.close < 1000),
    ("参考: 500円未満すべて", lambda j: j.close < 500),
    ("参考: 売買代金5億円未満すべて（株価に関係なく）", lambda j: j.to20 < 5 * OKU),
]


def topix500():
    found = [os.path.join(S.CACHE, f) for f in os.listdir(S.CACHE) if f.startswith("data_j.")]
    path = max(found, key=os.path.getmtime)
    df = pd.read_csv(path, dtype=str, encoding="cp932") if path.endswith(".csv") else pd.read_excel(path, dtype=str)
    size = df.get("規模区分", pd.Series("", index=df.index)).fillna("")
    keep = size.str.contains("Core30|Large70|Mid400")
    return set(df.loc[keep, "コード"].str.strip())


def turnover_at(prices, pairs):
    """(code, date) ごとの20日平均売買代金（その日まで）"""
    out = {}
    want = {}
    for c, d in pairs:
        want.setdefault(c, set()).add(d)
    for code, days in want.items():
        d = prices.get(code)
        if d is None or len(d) < 20:
            continue
        t = (d.Close.astype(float) * d.Volume.astype(float)).rolling(20).mean()
        idx = [x.date().isoformat() for x in d.index]
        m = dict(zip(idx, t.values))
        for day in days:
            v = m.get(day)
            if v is not None and not np.isnan(v):
                out[(code, day)] = float(v)
    return out


def stats(g):
    return dict(n=len(g), ok=g.ok.mean(), ex_all=g.ex_all.mean(), ex_sec=g.ex_sec.mean(),
                ex20=g.ex_pnl.mean(), ex20_med=g.ex_pnl.median(), win20=(g.ex_pnl > 0).mean() * 100)


def fmt(s, small=20):
    if s["n"] < small:
        return f'<td colspan="5" class="mut">{s["n"]}件</td>'
    c = lambda x: "up" if x > 0 else "dn"
    return (f'<td>{s["n"]:,}</td><td>{s["ok"]:.0f}%</td>'
            f'<td><span class="{c(s["ex_all"])}">{s["ex_all"]:+.1f}</span> / <span class="{c(s["ex_sec"])}">{s["ex_sec"]:+.1f}</span></td>'
            f'<td class="{c(s["ex20"])}">{s["ex20"]:+.1f}%</td><td class="{c(s["ex20_med"])}">{s["ex20_med"]:+.1f}%</td>')


def main():
    ev = pd.read_csv(os.path.join(OUT, "replay.csv"), dtype={"code": str, "date": str})
    j = ev[ev["first"].isin(["成功", "失敗（損切り）", "失敗（届かず）"])].copy()
    j["ok"] = (j["first"] == "成功") * 100.0
    with open(T.BASE_CACHE, encoding="utf-8") as f:
        b = json.load(f)
    ball = {k: v[0] for k, v in b["all"].items()}
    bsec = {tuple(k.split("|", 1)): v[0] for k, v in b["sec"].items()}
    j["base"] = j.date.map(ball)
    j["base_sec"] = [bsec.get((d, s), ball.get(d)) for d, s in zip(j.date, j.sector)]
    j = j.dropna(subset=["base"])
    j["ex_all"] = j.ok - j.base
    j["ex_sec"] = j.ok - j.base_sec

    uni = S.load_universe()
    t5 = topix500()
    prices = R.fetch(uni.code.tolist() + [T.BENCH])
    to = turnover_at(prices, zip(j.code, j.date))
    j["to20"] = [to.get((c, d), np.nan) for c, d in zip(j.code, j.date)]
    j = j.dropna(subset=["to20"])
    j["t500"] = j.code.isin(t5)
    j.to_csv(os.path.join(OUT, "lowprice.csv"), index=False, encoding="utf-8-sig")

    head = "<tr><th>件数</th><th>初動の成功率</th><th>全体差 / 同業種差</th><th>20日 TOPIX比 平均</th><th>中央値</th></tr>"
    sa = stats(j)
    allrow = (f'<p class="note">全体 {sa["n"]:,}件：成功率 {sa["ok"]:.0f}%、20日 TOPIX比 平均 {sa["ex20"]:+.1f}%・中央値 {sa["ex20_med"]:+.1f}%</p>')

    # 1. ルールごとに、外れる銘柄と残る銘柄
    rows = []
    for name, f in RULES:
        m = f(j)
        rows.append(f'<tr><td rowspan="2">{html.escape(name)}</td><td>外れる</td>{fmt(stats(j[m]))}</tr>'
                    f'<tr><td>残る</td>{fmt(stats(j[~m]))}</tr>')
    t1 = (f'<h2>1. 除外ルールの候補（全リスト合計）</h2><div class="wrap"><table>'
          f'<tr><th>ルール</th><th></th>{head[4:-5]}</tr>{"".join(rows)}</table></div>')

    # 2. 株価 × 売買代金
    j["pband"] = pd.cut(j.close, [0, 500, 1000, 3000, 1e9], labels=["500円未満", "500〜1,000円", "1,000〜3,000円", "3,000円以上"], right=False).astype(str)
    j["tband"] = pd.cut(j.to20, [0, 5 * OKU, 10 * OKU, 20 * OKU, 1e15], labels=["3〜5億", "5〜10億", "10〜20億", "20億以上"], right=False).astype(str)
    tb = ["3〜5億", "5〜10億", "10〜20億", "20億以上"]
    rows = []
    for p in ["500円未満", "500〜1,000円", "1,000〜3,000円", "3,000円以上"]:
        cells = []
        for t in tb:
            g = j[(j.pband == p) & (j.tband == t)]
            if len(g) < 20:
                cells.append(f'<td class="mut">{len(g)}件</td>')
                continue
            s = stats(g)
            c = lambda x: "up" if x > 0 else "dn"
            cells.append(f'<td>{s["ok"]:.0f}% <span class="{c(s["ex_all"])}">({s["ex_all"]:+.0f})</span>'
                         f'<div class="sub">20日 <span class="{c(s["ex20"])}">{s["ex20"]:+.1f}%</span>・{s["n"]}件</div></td>')
        rows.append(f"<tr><td>{p}</td>{''.join(cells)}</tr>")
    t2 = (f'<h2>2. 株価 × 20日平均売買代金</h2><p class="note">成功率（全体との差）／下段は20日保有のTOPIX比の平均と件数</p>'
          f'<div class="wrap"><table><tr><th>株価＼売買代金</th>{"".join(f"<th>{t}</th>" for t in tb)}</tr>{"".join(rows)}</table></div>')

    # 3. 1,000円未満を TOPIX500 か否かで
    rows = []
    for lab, m in [("1,000円未満・TOPIX500", (j.close < 1000) & j.t500),
                   ("1,000円未満・TOPIX500以外", (j.close < 1000) & ~j.t500),
                   ("1,000円以上", j.close >= 1000)]:
        rows.append(f"<tr><td>{lab}</td>{fmt(stats(j[m]))}</tr>")
    t3 = (f'<h2>3. 低位株を TOPIX500 かどうかで分ける</h2><div class="wrap"><table>'
          f'<tr><th></th>{head[4:-5]}</tr>{"".join(rows)}</table></div>')

    # 4. リスト別（本命のルール）
    rows = []
    f = RULES[1][1]
    for key, (_, name, _) in T.LISTS.items():
        g0 = j[j.list == key]
        if g0.empty:
            continue
        m = f(g0)
        rows.append(f'<tr><td rowspan="2">{html.escape(name)}</td><td>外れる</td>{fmt(stats(g0[m]))}</tr>'
                    f'<tr><td>残る</td>{fmt(stats(g0[~m]))}</tr>')
    t4 = (f'<h2>4. リスト別：{html.escape(RULES[1][0])}</h2><div class="wrap"><table>'
          f'<tr><th>リスト</th><th></th>{head[4:-5]}</tr>{"".join(rows)}</table></div>')

    page = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>低位株の除外検証</title>
<style>
:root{{--bg:#fafaf9;--fg:#1c1c1a;--mut:#6b6b66;--line:#e4e4e0;--card:#fff;--up:#d1343a;--dn:#1f8a55;--acc:#2a5bd7}}
@media (prefers-color-scheme:dark){{:root{{--bg:#141413;--fg:#ececea;--mut:#9a9a94;--line:#2c2c2a;--card:#1c1c1b;--up:#f06a6e;--dn:#4cc38a;--acc:#7aa2ff}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,"Hiragino Sans","Yu Gothic UI",sans-serif}}
main{{max-width:1100px;margin:0 auto;padding:20px 16px 60px}} h1{{font-size:21px;margin:0 0 4px}} h2{{font-size:16px;margin:28px 0 6px}}
.note,.sub{{color:var(--mut);font-size:12px}} .wrap{{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--card)}}
table{{border-collapse:collapse;width:100%;white-space:nowrap}} th,td{{padding:7px 10px;border-bottom:1px solid var(--line);text-align:right;vertical-align:top}}
th{{font-size:12px;color:var(--mut)}} td:first-child,th:first-child{{text-align:left}}
.up{{color:var(--up)}} .dn{{color:var(--dn)}} .mut{{color:var(--mut)}} a{{color:var(--acc)}}
</style></head><body><main>
<h1>低位株の除外検証</h1>
<p class="note">更新 {dt.datetime.now() + dt.timedelta(hours=9):%Y-%m-%d %H:%M}／{j.date.min()}〜{j.date.max()} の再現 {len(j):,}件／<a href="analysis.html">条件別検証へ</a></p>
<p class="note">売買代金はシグナルの日までの20日平均。TOPIX500は現在の規模区分で判定（当時とは異なる場合あり）。全体差・同業種差は初動の成功率の差（pt）。20日 TOPIX比は20営業日保有したときのTOPIXとの差。</p>
{allrow}{t1}{t2}{t3}{t4}
<p class="note">全件: <a href="lowprice.csv">lowprice.csv</a></p>
</main></body></html>"""
    with open(os.path.join(OUT, "lowprice.html"), "w", encoding="utf-8") as f:
        f.write(page)
    S.log(f"低位株の検証: {len(j):,}件 → docs/track/lowprice.html")


if __name__ == "__main__":
    main()
