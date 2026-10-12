# -*- coding: utf-8 -*-
"""
メール冒頭のウォッチ欄。
銘柄は GitHub の Secrets「WATCHLIST」から読む（例: "7203 6758@2500 9984"。@の後は取得単価で省略可）。
リポジトリは公開なので、ここで作った文面はファイルにもログにも残さず、送信するメールの中だけに入れる。
"""
import html
import json
import os
import re

PAGE_URL = "https://eplan4u-dotcom.github.io/kabu-screener/"


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
        out.append((code, cost))
    return out


def _pc(v):
    return "-" if v is None else f"{v:+.1f}%"


def _dist(p, ref):
    return None if not ref or p is None else (p / ref - 1) * 100


def build(raw, data_path="docs/data/all.json"):
    """(text, html) を返す。ウォッチが空なら ("", "")"""
    items = parse(raw)
    if not items:
        return "", ""
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
    for code, cost in items:
        x = stocks.get(code)
        if not x:
            T.append(f"・{code}  データなし")
            H.append(f"{code}　データなし<br><br>")
            continue
        p = x.get("p")
        lists = "・".join(k + (f"({x.get('dt')}日線)" if k == "D" and x.get("dt") else "") for k in x.get("L", ""))
        d25, d50, d10 = _dist(p, x.get("m25")), _dist(p, x.get("m50")), _dist(p, x.get("l10"))
        gain = (p / cost - 1) * 100 if cost and p else None
        mr = f"信用倍率{x['mr']:.2f}倍" if x.get("mr") is not None else ""
        # テキスト
        line1 = f"・{code} {x['n']}  終値{p:,.0f}（{x.get('ch', 0):+.1f}%）" + (f" 【{lists}】" if lists else "")
        line2 = (f"  25日線{_pc(d25)} 50日線{_pc(d50)} 10日安値{_pc(d10)}"
                 + (f" / 取得{cost:,.0f} 損益{_pc(gain)}" if gain is not None else "") + (f" / {mr}" if mr else ""))
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
            + (f'　<span style="color:{"#d1343a" if gain >= 0 else "#1f8a55"};">損益 {_pc(gain)}</span>' if gain is not None else "")
            + (f'<span style="color:#999;">　{mr}</span>' if mr else "") + "</span><br><br>")
    T.append("（%は終値から見た距離。マイナス＝その線・安値を下回っている）")
    H.append('<div style="font-size:12px;color:#999;">%は終値から見た距離。オレンジ＝その線・安値を下回っている（手仕舞いの目安）。</div>')
    return "\n".join(T) + "\n\n", "".join(H)
