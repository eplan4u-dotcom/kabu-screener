# -*- coding: utf-8 -*-
"""
過去日の再現：指定した期間の毎営業日について、「その日までの株価だけ」で A〜D・別枠を計算し、
当時のスクリーナーが出したはずの抽出結果を data/signals/replay.csv に書き出す。
成績は track.py が「再現」として実際の記録とは分けて集計する（docs/track/replay.html）。

使い方: python replay.py [開始日] [終了日]   例) python replay.py 2026-09-01 2026-10-08
注意
・銘柄一覧は現在のもの（その後に上場廃止した銘柄は含まれない）
・信用残は使わない（抽出条件には使っていないので結果は変わらない）
・D は直近1年のデータで条件を決めたため、この期間の再現は成績が良く出やすい
"""
import os, sys, time, pickle, datetime as dt

import numpy as np
import pandas as pd
import yfinance as yf

import screener as S

LOG = os.path.join(S.BASE, "data", "signals", "replay.csv")
COLS = ["date", "list", "code", "name", "sector", "close", "alert", "stop", "dtype", "rule", "ver"]
RULE = {"A": "alert", "B": "next_open", "C": "next_open", "D": "alert", "T": "next_open"}
DEFAULT_FROM, DEFAULT_TO = "2026-06-01", "2026-10-08"


def fetch(codes):
    """約2年分の日足（再現する日の1年前から計算できるように）"""
    os.makedirs(S.CACHE, exist_ok=True)
    cpath = os.path.join(S.CACHE, f"replay_prices2y_{dt.date.today()}.pkl")
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
    # 成績ページ（track.py）の「全体」の成功率の計算にも同じ株価を使う
    with open(os.path.join(S.CACHE, f"prices_{dt.date.today().isoformat()}.pkl"), "wb") as f:
        pickle.dump(data, f)
    return data


def snapshot(prices, day):
    """day の引け時点で見えていた株価（本番と同じく約1年分）。その日に売買のない銘柄は除く"""
    out = {}
    for code, d in prices.items():
        idx = d.index
        end = pd.Timestamp(day, tz=idx.tz)
        start = end - pd.Timedelta(days=365)
        j = idx.searchsorted(end, side="right")
        if j == 0 or idx[j - 1].normalize() != end:
            continue
        i = idx.searchsorted(start, side="right")
        out[code] = d.iloc[i:j]
    return out


def records(day, A, B, C, T, D):
    rows = []
    for key, x in (("A", A), ("B", B), ("C", C), ("D", D), ("T", T)):
        for _, r in x.iterrows():
            if key == "A":
                alert, stop = r.res, r.low10
            elif key == "D":
                alert, stop = r.d_alert, r.d_stop
            else:
                alert, stop = np.nan, r.low10
            rows.append(dict(date=day, list=key, code=str(r.code), name=r["name"], sector=r.sector,
                             close=round(float(r.close), 1), alert=round(float(alert), 1), stop=round(float(stop), 1),
                             dtype=str(r.d_type) if key == "D" else "", rule=RULE[key], ver="replay"))
    return rows


def main():
    d0 = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("REPLAY_FROM") or DEFAULT_FROM
    d1 = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("REPLAY_TO") or DEFAULT_TO
    t0 = time.time()
    uni = S.load_universe()
    S.log(f"対象銘柄: {len(uni):,}　再現期間: {d0}〜{d1}")
    prices = fetch(uni.code.tolist() + ["1306"])
    bench = prices.get("1306")
    if bench is None:
        S.log("TOPIX（1306）の株価が取れず、営業日が決められません")
        sys.exit(1)
    days = [x.date().isoformat() for x in bench.index if d0 <= x.date().isoformat() <= d1]
    S.log(f"営業日: {len(days)}日")
    rows, counts = [], []
    for day in days:
        S.ALL.clear()
        df, A, B, C, T, D = S.screen(uni, snapshot(prices, day))
        if df.empty:
            S.log(f"{day}: 分析できる銘柄なし")
            continue
        rows += records(day, A, B, C, T, D)
        counts.append((day, len(df), len(A), len(B), len(C), len(D), len(T)))
        S.log(f"{day}: 対象{len(df):,}  A{len(A)} B{len(B)} C{len(C)} D{len(D)} 別枠{len(T)}")
    out = pd.DataFrame(rows, columns=COLS)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    # 実際の記録がある日（本番の結果）は再現に含めない
    real = os.path.join(S.BASE, "data", "signals", "log.csv")
    if os.path.exists(real):
        have = set(pd.read_csv(real, dtype=str).date)
        out = out[~out.date.isin(have)]
    # 今回の期間外の再現結果は残す（期間を分けて実行しても積み上がる）
    if os.path.exists(LOG):
        old = pd.read_csv(LOG, dtype={"code": str, "date": str})
        out = pd.concat([old[(old.date < d0) | (old.date > d1)], out], ignore_index=True)
    out.sort_values(["date", "list", "code"]).to_csv(LOG, index=False, encoding="utf-8")
    cnt = pd.DataFrame(counts, columns=["date", "universe", "A", "B", "C", "D", "T"])
    cpath = os.path.join(os.path.dirname(LOG), "replay_counts.csv")
    if os.path.exists(cpath):
        oc = pd.read_csv(cpath, dtype={"date": str})
        cnt = pd.concat([oc[(oc.date < d0) | (oc.date > d1)], cnt], ignore_index=True).sort_values("date")
    cnt.to_csv(cpath, index=False, encoding="utf-8")
    S.log(f"\n完了（{time.time() - t0:.0f}秒）  再現 {len(out):,}件 → {LOG}")


if __name__ == "__main__":
    main()
