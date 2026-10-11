# -*- coding: utf-8 -*-
"""
JPX「銘柄別信用取引残高」（毎営業日16時頃・前営業日分・PDF）を読み取り、
銘柄ごとの 売残・買残・前日比 を返す。読み取った結果は data/margin/ に日付ごとのCSVで保存し、
前週比（約1週間前との比較）の計算に使う。失敗しても例外は外に出さず、空の結果を返す。
"""
import csv
import datetime as dt
import io
import os
import re
import urllib.parse
import urllib.request

PAGE = "https://www.jpx.co.jp/markets/statistics-equities/margin/01.html"
BASE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(BASE, "data", "margin")
KEEP = 15  # 保存しておく日数（ファイル数）

_SHS = re.compile(r"(\d{3}[0-9A-Z]0)\s+[A-Z]{2}[0-9A-Z]{9}\d株数 Shs\.\s+(.*)$")
_SHS_NOCODE = re.compile(r"株数 Shs\.\s+(.*)$")
_VAL = re.compile(r"(\d{3}[0-9A-Z]0)\s+[A-Z]{2}[0-9A-Z]{9}\d金額 Val\.")


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read()


def list_files():
    """一覧ページから [(日付, URL)] を新しい順に返す"""
    page = _get(PAGE).decode("utf-8", "ignore")
    out = {}
    for href in re.findall(r'href="([^"]*?(\d{8})_mtall\.pdf)"', page):
        url, ymd = href
        out[ymd] = urllib.parse.urljoin(PAGE, url)
    return sorted(out.items(), reverse=True)


def _nums(s):
    """'10,800 500 0.1% 165,800 ▲ 4,800 ...' → [10800, 500, '0.1%', 165800, -4800, ...]"""
    s = re.sub(r"▲\s*", "-", s)
    out = []
    for t in s.split():
        if t.endswith("%") or t == "*" or t == "-":
            out.append(t)
        else:
            try:
                out.append(int(t.replace(",", "")))
            except ValueError:
                out.append(t)
    return out


def parse_text(text):
    """PDFの文字列から {コード: (売残, 買残, 売残前日比, 買残前日比)} を作る"""
    res, pending = {}, None
    for line in text.splitlines():
        if "株数 Shs." in line:
            m = _SHS.search(line)
            body = m.group(2) if m else _SHS_NOCODE.search(line).group(1)
            n = _nums(body)
            vals = None
            if len(n) >= 5 and all(isinstance(n[i], int) for i in (0, 1, 3, 4)):
                vals = (n[0], n[3], n[1], n[4])
            if m and vals:
                res[m.group(1)[:4]] = vals
                pending = None
            else:
                pending = vals
        elif "金額 Val." in line and pending:
            m = _VAL.search(line)
            if m:
                res[m.group(1)[:4]] = pending
            pending = None
    return res


def parse_pdf(data):
    import pdfplumber
    parts = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for p in pdf.pages:
            parts.append(p.extract_text() or "")
    return parse_text("\n".join(parts))


def _save(ymd, rows):
    os.makedirs(DIR, exist_ok=True)
    with open(os.path.join(DIR, f"{ymd}.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["code", "sell", "buy", "dsell", "dbuy"])
        for code, v in sorted(rows.items()):
            w.writerow([code, *v])


def _read(ymd):
    rows = {}
    with open(os.path.join(DIR, f"{ymd}.csv"), encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows[r["code"]] = tuple(int(r[k]) for k in ("sell", "buy", "dsell", "dbuy"))
    return rows


def _cached():
    if not os.path.isdir(DIR):
        return []
    return sorted((f[:8] for f in os.listdir(DIR) if re.fullmatch(r"\d{8}\.csv", f)), reverse=True)


def update(log=print):
    """一覧にあって未保存のPDFを読み取り保存。古いものは削除"""
    have = set(_cached())
    for ymd, url in list_files():
        if ymd in have:
            continue
        try:
            rows = parse_pdf(_get(url))
            if len(rows) > 1000:          # 読み取りに失敗していないかの目安（通常は約4,000銘柄）
                _save(ymd, rows)
                log(f"  信用残 {ymd}: {len(rows):,}銘柄を読み取り")
            else:
                log(f"  信用残 {ymd}: 読み取り件数が少ないため保存しません（{len(rows)}件）")
        except Exception as e:
            log(f"  信用残 {ymd}: 読み取り失敗 {e}")
    for old in _cached()[KEEP:]:
        os.remove(os.path.join(DIR, f"{old}.csv"))


def load(log=print):
    """
    戻り値: (data, info)
      data[コード] = dict(sell, buy, dbuy, ratio, wbuy, wpct)
      info = dict(date='2026-10-08', base='2026-10-01')  ※取れなければ data={} info={}
    """
    try:
        update(log)
    except Exception as e:
        log(f"  信用残の一覧取得に失敗: {e}")
    days = _cached()
    if not days:
        return {}, {}
    latest = days[0]
    d_latest = dt.datetime.strptime(latest, "%Y%m%d").date()
    base = None
    for d in days[1:]:                                   # 7日以上前で一番新しいもの
        if (d_latest - dt.datetime.strptime(d, "%Y%m%d").date()).days >= 7:
            base = d
            break
    if base is None and len(days) > 1:                    # まだ1週間分たまっていなければ一番古いもの
        base = days[-1]
    cur = _read(latest)
    prev = _read(base) if base else {}
    data = {}
    for code, (sell, buy, dsell, dbuy) in cur.items():
        row = dict(sell=sell, buy=buy, dbuy=dbuy,
                   ratio=(buy / sell) if sell > 0 else None, wbuy=None, wpct=None)
        if code in prev:
            pb = prev[code][1]
            row["wbuy"] = buy - pb
            row["wpct"] = (buy - pb) / pb * 100 if pb > 0 else None
        data[code] = row
    fmt = lambda s: f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return data, dict(date=fmt(latest), base=fmt(base) if base else None)
