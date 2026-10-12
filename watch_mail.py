# -*- coding: utf-8 -*-
"""
メール冒頭のウォッチ欄。
銘柄は次の順で読む。
 1. Secrets「WATCHLIST_SHEET」に登録した Google スプレッドシート（列: コード・取得単価・株数・メモ）
    シートは「リンクを知っている全員が閲覧可」にしておく。アドレスでもIDだけでもよい
 2. 1が無いか読めないときは Secrets「WATCHLIST」（例: "7203 6758@2500 9984"。@の後は取得単価で省略可）
リポジトリは公開なので、ここで作った文面はファイルにもログにも残さず、送信するメールの中だけに入れる。
"""
import csv
import html
import io
import json
import os
import re
import urllib.request

PAGE_URL = "https://eplan4u-dotcom.github.io/kabu-screener/"


def _num(v):
    try:
        v = str(v).replace(",", "").replace("円", "").replace("株", "").strip()
        return float(v) if v else None
    except ValueError:
        return None


def from_sheet(ref):
    """Google スプレッドシートを CSV で読み、[(コード, 取得単価, 株数, メモ)] を返す。読めなければ None"""
    m = re.search(r"/d/([A-Za-z0-9_-]{20,})", ref or "") or re.fullmatch(r"\s*([A-Za-z0-9_-]{20,})\s*", ref or "")
    if not m:
        return None
    gid = re.search(r"[#&?]gid=(\d+)", ref or "")
    url = f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv&gid={gid.group(1) if gid else 0}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            text = r.read().decode("utf-8-sig")
    except Exception:
        return None
    if text.lstrip().startswith("<"):      # 共有されていないとログイン画面のHTMLが返る
        return None
    out = []
    for i, row in enumerate(csv.reader(io.StringIO(text))):
        if not row or i == 0 and not re.fullmatch(r"[0-9][0-9A-Za-z]{3}", row[0].strip()):
            continue                         # 見出し行・空行
        code = row[0].strip().upper()
        if not re.fullmatch(r"[0-9][0-9A-Z]{3}", code) or any(c == code for c, *_ in out):
            continue
        cell = lambda k: row[k] if len(row) > k else ""
        out.append((code, _num(cell(1)), _num(cell(2)), cell(3).strip()))
    return out


def parse(raw):
    out = []
    for tok in re.split(r"[\s,、]+", (raw or "").strip()):
        if not tok:
            continue
        code, _, cost = tok.partition("@")
        code = code.strip().upper()
        if not re.fullmatch(r"[0-9][0-9A-Z]{3}", code) or any(c == code for c, _ in out):
            continue
        try:
            cost = float(cost.replace(",", "")) if cost else None
        except ValueError:
            cost = None
        out.append((code, cost, None, ""))
    return out


def _pc(v):
    return "-" if v is None else f"{v:+.1f}%"


def _dist(p, ref):
    return None if not ref or p is None else (p / ref - 1) * 100


def build(raw, data_path="docs/data/all.json", sheet=""):
    """(text, html) を返す。ウォッチが空なら ("", "")"""
    note = ""
    items = from_sheet(sheet) if sheet else None
    if items is None:
        if sheet:
            note = "（スプレッドシートを読めなかったため、Secrets の WATCHLIST を表示しています。共有設定を確認してください）"
        items = parse(raw)
    if not items:
        return ("", "") if not note else (note + "\n\n", f'<div style="font-size:13px;color:#d97706;">{html.escape(note)}</div>')
    try:
        with open(data_path, encoding="utf-8") as f:
            data = json.load(f)
        stocks = {x["c"]: x for x in data["stocks"]}
    except Exception:
        stocks = {}
    E = html.escape
    T = [f"■ ★ ウォッチ {len(items)}銘柄"]
    H = [f'<div style="margin:18px 0 6px;padding:8px 12px;border-radius:8px;font-size:16px;font-weight:700;'
         f'color:#7c4a03;background:#fff6e0;">★ ウォッチ　{len(items)}銘柄</div><br>']
    if note:
        T.append(note)
        H.append(f'<div style="font-size:12px;color:#d97706;">{E(note)}</div>')
    for code, cost, qty, memo in items:
        x = stocks.get(code)
        if not x:
            T.append(f"・{code}  データなし")
            H.append(f"{code}　データなし<br><br>")
            continue
        p = x.get("p")
        lists = "・".join(k + (f"({x.get('dt')}日線)" if k == "D" and x.get("dt") else "") for k in x.get("L", ""))
        d25, d50, d10 = _dist(p, x.get("m25")), _dist(p, x.get("m50")), _dist(p, x.get("l10"))
        gain = (p / cost - 1) * 100 if cost and p else None
        amt = (p - cost) * qty if gain is not None and qty else None
        mr = f"信用倍率{x['mr']:.2f}倍" if x.get("mr") is not None else ""
        # テキスト
        line1 = f"・{code} {x['n']}  終値{p:,.0f}（{x.get('ch', 0):+.1f}%）" + (f" 【{lists}】" if lists else "")
        line2 = (f"  25日線{_pc(d25)} 50日線{_pc(d50)} 10日安値{_pc(d10)}"
                 + (f" / 取得{cost:,.0f} 損益{_pc(gain)}" if gain is not None else "")
                 + (f"（{amt:+,.0f}円）" if amt is not None else "") + (f" / {mr}" if mr else "")
                 + (f"\n  メモ: {memo}" if memo else ""))
        T += [line1, line2]
        # HTML（1行目: 銘柄・終値・該当リスト／2行目: 線までの距離・損益・信用）
        def col(v):
            return "#d97706" if v is not None and v < 0 else "#444"
        badge = f'　<span style="background:#eee;border-radius:5px;padding:0 5px;font-size:12px;">{E(lists)}</span>' if lists else ""
        chc = "#d1343a" if (x.get("ch") or 0) >= 0 else "#1f8a55"
        H.append(
            f'<a href="https://shikiho.toyokeizai.net/stocks/{code}" style="color:#1a56db;text-decoration:none;font-weight:600;">{E(x["n"])}</a>'
            f' <a href="https://jp.tradingview.com/chart/?symbol=TSE:{code}" style="color:#888;font-size:13px;text-decoration:none;">{code}</a>'
            f'　終値 <b>{p:,.0f}</b> <span style="color:{chc};">{x.get("ch", 0):+.1f}%</span>{badge}<br>'
            f'<span style="font-size:13px;"><span style="color:{col(d25)};">25日線 {_pc(d25)}</span>'
            f'　<span style="color:{col(d50)};">50日線 {_pc(d50)}</span>'
            f'　<span style="color:{col(d10)};">10日安値 {_pc(d10)}</span>'
            + (f'　<span style="color:{"#d1343a" if gain >= 0 else "#1f8a55"};">損益 {_pc(gain)}'
               + (f'（{amt:+,.0f}円）' if amt is not None else "") + '</span>' if gain is not None else "")
            + (f'<span style="color:#999;">　{mr}</span>' if mr else "") + "</span>"
            + (f'<br><span style="font-size:12px;color:#888;">メモ: {E(memo)}</span>' if memo else "") + "<br><br>")
    T.append("（%は終値から見た距離。マイナス＝その線・安値を下回っている）")
    H.append('<div style="font-size:12px;color:#999;">%は終値から見た距離。オレンジ＝その線・安値を下回っている（手仕舞いの目安）。</div>')
    return "\n".join(T) + "\n\n", "".join(H)
