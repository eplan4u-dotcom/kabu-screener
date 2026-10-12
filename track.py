# -*- coding: utf-8 -*-
"""
抽出結果のフォワードテスト（実際の取引とは切り離した、スクリーニング自体の成績）。

1. 毎日の結果CSV（docs/A_…_日付.csv など）を data/signals/log.csv に取り込む
2. 過去に出た銘柄の「その後」を、決まったルールで毎日計算し直す
   買い : A・D … 出てから5営業日以内にアラートを超えたら、その価格（窓を開けたら始値）で買い
          B・C・別枠 … 翌営業日の始値で買い
   売り : 損切り目安（A・B・C・別枠＝10日安値、D＝タッチ〜3本目の最安値）を割ったら損切り
          （窓を開けて下回ったら始値）。割らなければ、買った日から20営業日後の終値
   比較 : 同じ期間の TOPIX（1306 TOPIX連動ETF）の騰落との差＝超過
3. docs/track/index.html に成績ページを出す

ルールを途中で変えると比較できなくなるため、変える場合は RULE_VERSION を上げ、版ごとに集計する。
"""
import os, glob, re, html, pickle, time, datetime as dt

import numpy as np
import pandas as pd
import yfinance as yf

BASE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.environ.get("SCREENER_OUT") or os.path.join(BASE, "docs")
LOG = os.environ.get("TRACK_LOG") or os.path.join(BASE, "data", "signals", "log.csv")
OUT = os.path.join(DOCS, "track")
CACHE = os.path.join(BASE, "cache")

RULE_VERSION = 1
ALERT_DAYS = 5      # アラート型（A・D）の待ち日数
HOLD = 20           # 保有日数（営業日）
BENCH = "1306"      # TOPIX連動ETF
SHOCK_DAY = 2.0     # 地合い急変：その日のTOPIXが±2%以上
SHOCK_5D = -5.0     # 地合い急変：直近5日のTOPIXが-5%以下

LISTS = {  # 記号, ファイル名の頭, 表示名, 買い方
    "A": ("A_breakout_watch", "A. ブレイク待ち", "alert"),
    "B": ("B_breakout_today", "B. 当日ブレイク", "next_open"),
    "C": ("C_quiet_streak", "C. 静かな連騰", "next_open"),
    "D": ("D_pullback_3up", "D. 押し目3連陽", "alert"),
    "T": ("T_theme_spike", "別枠. テーマ急騰", "next_open"),
}
COLS = ["date", "list", "code", "name", "sector", "close", "alert", "stop", "dtype", "rule", "ver"]


def log(m):
    print(m, flush=True)


# ---------- 1. 取り込み ----------
def ingest():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    old = pd.read_csv(LOG, dtype={"code": str, "date": str}) if os.path.exists(LOG) else pd.DataFrame(columns=COLS)
    new = []
    files = []
    for key, (head, _, _) in LISTS.items():
        for f in glob.glob(os.path.join(DOCS, f"{head}_*.csv")):
            m = re.search(r"_(\d{4}-\d{2}-\d{2})\.csv$", f)
            if m:
                files.append((m.group(1), key, f))
    if not files:
        return old
    latest = max(d for d, _, _ in files)
    have = set(old.date.unique())
    ver = (os.environ.get("GITHUB_SHA") or "")[:7]
    for day, key, f in files:
        if day in have and day != latest:       # 取り込み済み。最新日だけは再実行に合わせて入れ直す
            continue
        try:
            x = pd.read_csv(f, dtype={"code": str})
        except Exception:
            continue
        for _, r in x.iterrows():
            if key == "A":
                alert, stop = r.get("res"), r.get("low10")
            elif key == "D":
                alert, stop = r.get("d_alert"), r.get("d_stop")
            else:
                alert, stop = np.nan, r.get("low10")
            new.append(dict(date=day, list=key, code=str(r.code), name=r.get("name", ""), sector=r.get("sector", ""),
                            close=r.close, alert=alert, stop=stop,
                            dtype=str(int(float(r.get("d_type")))) if key == "D" and pd.notna(r.get("d_type")) else "",
                            rule=LISTS[key][2], ver=ver))
    if not new:
        return old
    new = pd.DataFrame(new)
    refresh = set(new.date.unique())
    keep = old[~old.date.isin(refresh)]
    out = pd.concat([keep, new], ignore_index=True).drop_duplicates(["date", "list", "code"], keep="last")
    out = out.sort_values(["date", "list", "code"]).reset_index(drop=True)
    out[COLS].to_csv(LOG, index=False, encoding="utf-8")
    log(f"記録: {len(out):,}件（今回 {len(new):,}件を取り込み）")
    return out


# ---------- 2. 株価 ----------
def prices(codes):
    data = {}
    today = dt.date.today().isoformat()
    cpath = os.path.join(CACHE, f"prices_{today}.pkl")     # 同じ実行でスクリーナーが取得した株価を使う
    if os.path.exists(cpath):
        with open(cpath, "rb") as f:
            cached = pickle.load(f)
        data = {c: cached[c] for c in codes if c in cached}
    miss = [c for c in codes if c not in data]
    for i in range(0, len(miss), 100):
        part = [f"{c}.T" for c in miss[i:i + 100]]
        try:
            df = yf.download(part, period="1y", interval="1d", group_by="ticker",
                             auto_adjust=False, threads=True, progress=False)
        except Exception as e:
            log(f"株価取得に失敗: {e}")
            continue
        for t in part:
            try:
                d = df[t] if isinstance(df.columns, pd.MultiIndex) else df
                d = d[["Open", "High", "Low", "Close", "Volume"]].dropna()
                if len(d):
                    data[t[:-2]] = d
            except Exception:
                pass
        time.sleep(1)
    return data


# ---------- 3. 評価 ----------
def evaluate(sig, px):
    bench = px.get(BENCH)
    bc = bench.Close if bench is not None else None
    rows = []
    for _, s in sig.iterrows():
        d = px.get(s.code)
        r = s.to_dict()
        r.update(status="データなし")
        if d is None or len(d) == 0:
            rows.append(r)
            continue
        idx = [x.date().isoformat() for x in d.index]
        if s.date not in idx:
            rows.append(r)
            continue
        t = idx.index(s.date)
        o, h, l, c = (d[k].astype(float).values for k in ("Open", "High", "Low", "Close"))
        n = len(c)
        stop = float(s.stop) if pd.notna(s.stop) else np.nan
        # 終値基準（ルールなし）の騰落
        for k in (5, 10, 20):
            r[f"ret{k}"] = (c[t + k] / c[t] - 1) * 100 if t + k < n else np.nan
        if bc is not None and t + 20 < n:
            b0, b1 = bc.get(d.index[t]), bc.get(d.index[t + 20])
            r["ex_close20"] = r["ret20"] - ((b1 / b0 - 1) * 100) if b0 and b1 else np.nan
        # 買い
        entry_i = entry = None
        if s.rule == "alert":
            alert = float(s.alert)
            for i in range(t + 1, min(t + 1 + ALERT_DAYS, n)):
                if h[i] > alert:
                    entry_i, entry = i, max(alert, o[i])
                    break
            if entry_i is None:
                r["status"] = "アラート待ち" if t + ALERT_DAYS >= n else "未到達"
                r["days"] = n - 1 - t
                rows.append(r)
                continue
        else:
            if t + 1 >= n:
                r["status"], r["days"] = "翌日待ち", 0
                rows.append(r)
                continue
            entry_i, entry = t + 1, o[t + 1]
        r["entry_date"], r["entry"] = idx[entry_i], entry
        # 売り
        exit_i = exit_p = None
        reason = None
        last = min(entry_i + HOLD, n - 1)
        for j in range(entry_i, last + 1):
            if not np.isnan(stop) and l[j] < stop:
                exit_i = j
                exit_p = o[j] if (j > entry_i and o[j] < stop) else stop
                reason = "損切り"
                break
        if exit_i is None:
            if entry_i + HOLD < n:
                exit_i, exit_p, reason = entry_i + HOLD, c[entry_i + HOLD], "20日満了"
            else:
                exit_i, exit_p, reason = n - 1, c[n - 1], "保有中"
        r["status"] = reason
        r["exit_date"], r["exit"] = idx[exit_i], exit_p
        r["pnl"] = (exit_p / entry - 1) * 100
        r["maxup"] = (max(h[entry_i:exit_i + 1]) / entry - 1) * 100
        r["days"] = exit_i - entry_i
        if bc is not None:
            b0 = bc.get(d.index[entry_i - 1]) if entry_i >= 1 else None
            b1 = bc.get(d.index[exit_i])
            r["ex_pnl"] = r["pnl"] - ((b1 / b0 - 1) * 100) if b0 and b1 else np.nan
        rows.append(r)
    out = pd.DataFrame(rows)
    # 地合い急変の印（シグナルの日）
    out["shock"] = False
    if bc is not None:
        bdates = [x.date().isoformat() for x in bc.index]
        chg = bc.pct_change() * 100
        chg5 = (bc / bc.shift(5) - 1) * 100
        flag = {dd: (abs(a) >= SHOCK_DAY) or (b <= SHOCK_5D) for dd, a, b in zip(bdates, chg.values, chg5.values)
                if not (np.isnan(a) or np.isnan(b))}
        out["shock"] = out.date.map(lambda x: flag.get(x, False))
    return out


# ---------- 4. ページ ----------
def pct(x, nd=1):
    return "-" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:+.{nd}f}%"


def summary_rows(ev):
    E = html.escape
    groups = []
    for key, (_, label, rule) in LISTS.items():
        g = ev[ev.list == key]
        groups.append((label, g, rule))
        if key == "D":
            for tp in ("25", "50", "75"):
                groups.append((f"　{tp}日線型", g[g.dtype.astype(str) == tp], rule))
    rows = []
    for label, g, rule in groups:
        done = g[g.status.isin(["損切り", "20日満了"])]
        judged = g[~g.status.isin(["アラート待ち", "翌日待ち", "データなし"])]
        hit = g[g.status.isin(["損切り", "20日満了", "保有中"])]
        hit_rate = f"{len(hit) / len(judged) * 100:.0f}%" if (rule == "alert" and len(judged)) else "-"
        c20 = g["ex_close20"].dropna() if "ex_close20" in g else pd.Series(dtype=float)
        cells = [E(label), f"{len(g):,}", f"{len(done):,}", hit_rate]
        if len(done):
            cells += [f"{(done.pnl > 0).mean() * 100:.0f}%", f"<b>{pct(done.pnl.mean())}</b>", pct(done.ex_pnl.mean()),
                      f"{(done.status == '損切り').mean() * 100:.0f}%", pct(done.maxup.mean())]
        else:
            cells += ["-"] * 5
        cells += [pct(c20.mean()) if len(c20) else "-", f"{len(c20)}"]
        rows.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    return "".join(rows)


def write(ev):
    os.makedirs(OUT, exist_ok=True)
    E = html.escape
    ev.to_csv(os.path.join(OUT, "track.csv"), index=False, encoding="utf-8-sig")
    head = ("<tr><th>リスト</th><th>件数</th><th>決済済み</th><th>アラート到達</th><th>勝率</th><th>平均損益</th>"
            "<th>TOPIX比</th><th>損切り率</th><th>最大上昇</th><th>終値20日後 TOPIX比</th><th>（件数）</th></tr>")
    normal = ev[~ev.shock]
    shock_days = sorted(ev[ev.shock].date.unique())

    rec = ev.sort_values(["date", "list"], ascending=[False, True])
    recent_days = sorted(ev.date.unique())[-30:]
    rec = rec[rec.date.isin(recent_days)]
    cls = {"損切り": "dn", "20日満了": "", "保有中": "up", "アラート待ち": "mut", "未到達": "mut", "翌日待ち": "mut"}
    rows = []
    for _, r in rec.iterrows():
        tv = f"https://jp.tradingview.com/chart/?symbol=TSE:{r.code}"
        dtp = f"{r.dtype}日線" if r.list == "D" and r.dtype else ""
        entry = (f'{r.entry:,.0f}<div class="sub">{str(r.entry_date)[5:]}</div>'
                 if pd.notna(r.get("entry")) else "-")
        pnl = r.get("pnl")
        rows.append(
            f'<tr><td><a class="nm" href="https://shikiho.toyokeizai.net/stocks/{r.code}" target="_blank">{E(str(r["name"]))}</a>'
            f'<div class="sub"><a class="nm" href="{tv}" target="_blank">{r.code}</a></div></td>'
            f'<td>{r.date[5:]}{" ⚡" if r.shock else ""}</td><td>{r.list}{(" " + dtp) if dtp else ""}</td>'
            f'<td>{r.close:,.0f}</td><td>{"-" if pd.isna(r.alert) else f"{r.alert:,.0f}"}</td>'
            f'<td>{"-" if pd.isna(r.stop) else f"{r.stop:,.0f}"}</td><td>{entry}</td>'
            f'<td class="{cls.get(r.status, "")}">{E(str(r.status))}</td>'
            f'<td class="{"up" if pd.notna(pnl) and pnl >= 0 else "dn"}">{pct(pnl)}</td><td>{pct(r.get("maxup"))}</td>'
            f'<td>{"-" if pd.isna(r.get("days")) else int(r.get("days"))}日</td></tr>')

    page = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>スクリーナー成績</title>
<style>
:root{{--bg:#fafaf9;--fg:#1c1c1a;--mut:#6b6b66;--line:#e4e4e0;--card:#fff;--up:#d1343a;--dn:#1f8a55;--acc:#2a5bd7}}
@media (prefers-color-scheme:dark){{:root{{--bg:#141413;--fg:#ececea;--mut:#9a9a94;--line:#2c2c2a;--card:#1c1c1b;--up:#f06a6e;--dn:#4cc38a;--acc:#7aa2ff}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,"Hiragino Sans","Yu Gothic UI",sans-serif}}
main{{max-width:1300px;margin:0 auto;padding:20px 16px 60px}} h1{{font-size:21px;margin:0 0 4px}} h2{{font-size:17px;margin:30px 0 4px}}
.note,.sub{{color:var(--mut);font-size:12px}} .wrap{{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--card)}}
table{{border-collapse:collapse;width:100%;white-space:nowrap}} th,td{{padding:7px 10px;border-bottom:1px solid var(--line);text-align:right;vertical-align:middle}}
th{{font-size:12px;color:var(--mut)}} td:first-child,th:first-child{{text-align:left;position:sticky;left:0;background:var(--card);z-index:1}}
.up{{color:var(--up)}} .dn{{color:var(--dn)}} .mut{{color:var(--mut)}} a{{color:var(--acc)}} a.nm{{color:inherit}}
</style></head><body><main>
<h1>スクリーナーの成績（フォワードテスト）</h1>
<p class="note">更新 {dt.datetime.now() + dt.timedelta(hours=9):%Y-%m-%d %H:%M}／記録 {len(ev):,}件（{ev.date.min() if len(ev) else "-"}〜{ev.date.max() if len(ev) else "-"}）／ルール 第{RULE_VERSION}版／<a href="../">結果ページへ</a></p>
<p class="note">実際の取引とは別に、抽出した銘柄を決まったルールで機械的に売買した場合の成績です。
買い：A・Dは出てから{ALERT_DAYS}営業日以内にアラートを超えたら、その価格で買い（窓を開けたら始値）。B・C・別枠は翌営業日の始値。
売り：損切り目安（A・B・C・別枠＝10日安値、D＝タッチから3本目までの最安値）を割ったら損切り、割らなければ{HOLD}営業日後の終値。
「TOPIX比」は同じ期間のTOPIX（1306）の騰落を引いた値で、地合いの影響を除いた実力です。
「終値20日後」はルールを使わず、抽出日の終値から20営業日後までの騰落のTOPIX比です。判断の目安は、リストごとに50〜100件の決済がたまってからです。</p>

<h2>成績（全期間）</h2>
<div class="wrap"><table>{head}{summary_rows(ev)}</table></div>

<h2>成績（地合い急変日を除く）</h2>
<p class="note">地合い急変日＝TOPIXがその日±{SHOCK_DAY:.0f}%以上、または直近5日で{SHOCK_5D:.0f}%以下の日に出た銘柄（下の一覧に⚡）。
大きな下落や一斉反発のような外的要因を除いた、ふだんの成績です。該当日: {E("、".join(d[5:] for d in shock_days)) or "なし"}</p>
<div class="wrap"><table>{head}{summary_rows(normal)}</table></div>

<h2>直近30営業日の銘柄ごとの経過</h2>
<div class="wrap"><table><tr><th>銘柄</th><th>抽出日</th><th>リスト</th><th>抽出時の終値</th><th>アラート</th><th>損切り目安</th><th>買値</th><th>状態</th><th>損益</th><th>最大上昇</th><th>経過</th></tr>{"".join(rows)}</table></div>
<p class="note">全件: <a href="track.csv">track.csv</a>／記録: data/signals/log.csv</p>
</main></body></html>"""
    with open(os.path.join(OUT, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)


def main():
    sig = ingest()
    if sig.empty:
        log("記録がありません")
        return
    sig["code"] = sig.code.astype(str)
    sig["date"] = sig.date.astype(str)
    sig["dtype"] = sig.dtype.fillna("").astype(str).str.replace(".0", "", regex=False)
    px = prices(sorted(set(sig.code)) + [BENCH])
    ev = evaluate(sig, px)
    write(ev)
    log(f"成績ページを更新: {len(ev):,}件")


if __name__ == "__main__":
    main()
