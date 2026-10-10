# -*- coding: utf-8 -*-
"""docs/summary.txt を Gmail で送る。GMAIL_USER / GMAIL_APP_PASSWORD が未設定なら何もしない"""
import os, smtplib, sys
from email.mime.text import MIMEText
from email.header import Header

user = os.environ.get("GMAIL_USER", "").strip()
pw = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
to = os.environ.get("MAIL_TO", "").strip() or user
if not user or not pw:
    print("メール設定（GMAIL_USER / GMAIL_APP_PASSWORD）が未登録のため送信をスキップしました")
    sys.exit(0)
body = open("docs/summary.txt", encoding="utf-8").read()
subject = open("docs/subject.txt", encoding="utf-8").read().strip()
msg = MIMEText(body, "plain", "utf-8")
msg["Subject"] = Header(subject, "utf-8")
msg["From"] = user
msg["To"] = to
with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
    s.login(user, pw)
    s.sendmail(user, [to], msg.as_string())
print(f"メール送信: {to}")
