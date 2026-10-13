# -*- coding: utf-8 -*-
"""docs/summary.txt を Gmail で送る。GMAIL_USER / GMAIL_APP_PASSWORD が未設定なら何もしない"""
import os, smtplib, sys, glob
from email.mime.base import MIMEBase
from email import encoders
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header

user = os.environ.get("GMAIL_USER", "").strip()
pw = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
to = os.environ.get("MAIL_TO", "").strip() or user
if not user or not pw:
    print("メール設定（GMAIL_USER / GMAIL_APP_PASSWORD）が未登録のため送信をスキップしました")
    sys.exit(0)
body = open("docs/summary.txt", encoding="utf-8").read()
subject = open("docs/subject.txt", encoding="utf-8").read().strip()
body_html = open("docs/summary.html", encoding="utf-8").read() if os.path.exists("docs/summary.html") else ""

# ウォッチ欄（Secrets の WATCHLIST）。公開リポジトリなので、ファイルにもログにも残さずメールの中だけに入れる
try:
    import watch_mail
    w_text, w_html = watch_mail.build(os.environ.get("WATCHLIST", ""), sheet=os.environ.get("WATCHLIST_SHEET", ""))
except Exception as e:
    w_text, w_html = "", ""
    print(f"ウォッチ欄の作成に失敗（ウォッチ欄なしで送信）: {type(e).__name__}")
if w_text:
    i = body.find("\n■ A.")
    body = body[:i + 1] + w_text + body[i + 1:] if i >= 0 else w_text + body
if w_html and body_html:
    mark = "結果ページを開く</a><br>"
    body_html = body_html.replace(mark, mark + w_html, 1) if mark in body_html else w_html + body_html

alt = MIMEMultipart("alternative")
alt.attach(MIMEText(body, "plain", "utf-8"))
if body_html:
    alt.attach(MIMEText(body_html, "html", "utf-8"))
msg = MIMEMultipart("mixed")
msg.attach(alt)
# HYPER SBI 2 取込用CSV（screener.py が docs/hypersbi/ に出力）を添付
for p in sorted(glob.glob("docs/hypersbi/*.csv")):
    part = MIMEBase("text", "csv")
    part.set_payload(open(p, "rb").read())
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", "attachment", filename=os.path.basename(p))
    msg.attach(part)
msg["Subject"] = Header(subject, "utf-8")
msg["From"] = user
msg["To"] = to
with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
    s.login(user, pw)
    s.sendmail(user, [to], msg.as_string())
print(f"メール送信: {to}")
