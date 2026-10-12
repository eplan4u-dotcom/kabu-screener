# -*- coding: utf-8 -*-
"""
再現（docs/track/replay.csv）の初動の成功率を、条件ごとに分けて検証する。
  1. 地合い  … TOPIX（1306）が25日線の上か下か／直近20日の騰落
  2. 件数    … その日・そのリストの件数
  3. 業種の強さ … その日の業種（東証33業種）の直近20日騰落（売買代金3億円以上の銘柄の中央値）の順位
各区分で「成功率」「全体との差」「同業種との差」を出す（基準は data/signals/base_rates.json）。
結果は docs/track/analysis.html と analysis.csv。本番の抽出には影響しない。
"""
import os, json, html, datetime as dt

import numpy as np
import pandas as pd

import screener as S
import replay as R
import track as T

OUT = os.path.join(S.BASE, "docs", "track")


def topix_regime(bench):
    b = T.adjust_splits(bench)
    c = b.Close.astype(float)
    ma25 = c.rolling(25).mean()
    r20 = (c / c.shift(20) - 1) * 100
    idx = [x.date().isoformat() for x in b.index]
    return {d: dict(above25=bool(c.iloc[i] > ma25.iloc[i]) if not np.isnan(ma25.iloc[i]) else None,
                    r20=float(r20.iloc[i]) if not np.isnan(r20.iloc[i]) else None)
            for i, d in enumerate(idx)}


def sector_strength(prices, uni, dates):
    """日付ごとに、業種の20日騰落（中央値）の順位（1＝最強）と業種数"""
    sec = dict(zip(uni.code, uni.sector))
    want = set(dates)
    rec = []
    for code, d in prices.items():
        s = sec.get(code)
        if not s or len(d) < 30:
            continue
        d = T.adjust_splits(d)
        c, v = d.Close.astype(float).values, d.Volume.astype(float).values
        turn = pd.Series(c * v).rolling(20).mean().values
        for i, x in enumerate(d.index):
            day = x.date().isoformat()
            if day in want and i >= 20 and turn[i] >= S.CFG["min_turnover"]:
                rec.append((day, s, (c[i] / c[i - 20] - 1) * 100))
    df = pd.DataFrame(rec, columns=["date", "sector", "r20"])
    g = df.groupby(["date", "sector"]).r20.agg(["median", "count"]).reset_index()
    g = g[g["count"] >= 3]
    g["rank"] = g.groupby("date")["median"].rank(ascending=False, method="min")
    g["nsec"] = g.groupby("date")["median"].transform("count")
    return {(r.date, r.sector): (int(r["rank"]), int(r.nsec), float(r["median"])) for _, r in g.iterrows()}


def table(j, col, order, label):
    rows = []
    for key, (_, name, _) in T.LISTS.items():
        g0 = j[j.list == key]
        if g0.empty:
            continue
        cells = [html.escape(name)]
        for b in order:
            g = g0[g0[col] == b]
            if len(g) < 20:
                cells.append(f'<span class="mut">{len(g)}件</span>')
                continue
            ex = g.ex_all.mean()
            exs = g.ex_sec.mean()
            cells.append(f'{g.ok.mean():.0f}%<div class="sub"><span class="{"up" if ex > 0 else "dn"}">{ex:+.0f}</span>'
                         f' / <span class="{"up" if exs > 0 else "dn"}">{exs:+.0f}</span>pt</div><div class="sub">{len(g)}件</div>')
        rows.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    head = "<tr><th>リスト</th>" + "".join(f"<th>{html.escape(str(b))}</th>" for b in order) + "</tr>"
    return f"<h2>{html.escape(label)}</h2><div class=\"wrap\"><table>{head}{''.join(rows)}</table></div>"


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
    prices = R.fetch(uni.code.tolist() + [T.BENCH])
    reg = topix_regime(prices[T.BENCH])
    j["ma25"] = j.date.map(lambda d: {True: "TOPIX 25日線の上", False: "TOPIX 25日線の下"}.get((reg.get(d) or {}).get("above25"), "不明"))
    r20 = j.date.map(lambda d: (reg.get(d) or {}).get("r20"))
    j["topix20"] = pd.cut(r20, [-99, -3, 0, 3, 6, 99], labels=["-3%未満", "-3〜0%", "0〜3%", "3〜6%", "6%以上"]).astype(str)
    # 件数は判定済みでなく、その日に出た全件で数える
    nall = ev.groupby(["date", "list"]).code.count()
    j["n_day"] = [nall.get((d, l), 0) for d, l in zip(j.date, j.list)]
    j["n_bin"] = pd.cut(j.n_day, [0, 3, 8, 15, 999], labels=["1〜3件", "4〜8件", "9〜15件", "16件以上"]).astype(str)
    ss = sector_strength(prices, uni, sorted(set(j.date)))
    rk = [ss.get((d, s)) for d, s in zip(j.date, j.sector)]
    j["sec_rank"] = [x[0] if x else np.nan for x in rk]
    j["sec_pct"] = [x[0] / x[1] if x else np.nan for x in rk]
    j["sec_bin"] = pd.cut(j.sec_pct, [0, 0.2, 0.4, 0.6, 0.8, 1.0],
                          labels=["上位20%", "20〜40%", "40〜60%", "60〜80%", "下位20%"]).astype(str)
    j["m"] = j.date.str[5:7].astype(int)
    j["season"] = j.m.map(lambda m: "6〜9月" if 6 <= m <= 9 else "10〜5月")
    # 組み合わせ：地合い×業種
    j["combo"] = np.where(j.ma25 == "TOPIX 25日線の上", "地合い○", "地合い×") + "・" + \
        np.where(j.sec_pct <= 0.4, "業種上位", "業種それ以外")
    j.drop(columns=[c for c in ("m",) if c in j]).to_csv(os.path.join(OUT, "analysis.csv"), index=False, encoding="utf-8-sig")

    parts = [
        table(j, "ma25", ["TOPIX 25日線の上", "TOPIX 25日線の下"], "1. 地合い：TOPIXが25日線の上か下か"),
        table(j, "topix20", ["-3%未満", "-3〜0%", "0〜3%", "3〜6%", "6%以上"], "1'. 地合い：TOPIXの直近20日の騰落"),
        table(j, "season", ["10〜5月", "6〜9月"], "参考：季節"),
        table(j, "n_bin", ["1〜3件", "4〜8件", "9〜15件", "16件以上"], "2. その日・そのリストの件数"),
        table(j, "sec_bin", ["上位20%", "20〜40%", "40〜60%", "60〜80%", "下位20%"], "3. 業種の強さ（直近20日の騰落の順位）"),
        table(j, "combo", ["地合い○・業種上位", "地合い○・業種それ以外", "地合い×・業種上位", "地合い×・業種それ以外"],
              "組み合わせ：地合い×業種の強さ"),
    ]
    page = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>再現の条件別検証</title>
<style>
:root{{--bg:#fafaf9;--fg:#1c1c1a;--mut:#6b6b66;--line:#e4e4e0;--card:#fff;--up:#d1343a;--dn:#1f8a55;--acc:#2a5bd7}}
@media (prefers-color-scheme:dark){{:root{{--bg:#141413;--fg:#ececea;--mut:#9a9a94;--line:#2c2c2a;--card:#1c1c1b;--up:#f06a6e;--dn:#4cc38a;--acc:#7aa2ff}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,"Hiragino Sans","Yu Gothic UI",sans-serif}}
main{{max-width:1100px;margin:0 auto;padding:20px 16px 60px}} h1{{font-size:21px;margin:0 0 4px}} h2{{font-size:16px;margin:28px 0 6px}}
.note,.sub{{color:var(--mut);font-size:12px}} .wrap{{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--card)}}
table{{border-collapse:collapse;width:100%;white-space:nowrap}} th,td{{padding:7px 10px;border-bottom:1px solid var(--line);text-align:right;vertical-align:top}}
th{{font-size:12px;color:var(--mut)}} td:first-child,th:first-child{{text-align:left;position:sticky;left:0;background:var(--card)}}
.up{{color:var(--up)}} .dn{{color:var(--dn)}} .mut{{color:var(--mut)}} a{{color:var(--acc)}}
</style></head><body><main>
<h1>再現の条件別検証</h1>
<p class="note">更新 {dt.datetime.now() + dt.timedelta(hours=9):%Y-%m-%d %H:%M}／{j.date.min()}〜{j.date.max()} の再現 {len(j):,}件（初動の判定済み）／<a href="replay.html">再現の成績へ</a></p>
<p class="note">各セル：初動の成功率／下段は「全体との差 / 同業種との差」（pt）と件数。全体との差は、同じ日の全銘柄の成功率との差なので、地合いそのものの良し悪しは差し引かれています。20件未満は件数のみ。</p>
{''.join(parts)}
<p class="note">全件: <a href="analysis.csv">analysis.csv</a></p>
</main></body></html>"""
    with open(os.path.join(OUT, "analysis.html"), "w", encoding="utf-8") as f:
        f.write(page)
    S.log(f"条件別検証: {len(j):,}件 → docs/track/analysis.html")


if __name__ == "__main__":
    main()
