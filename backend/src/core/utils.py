import os
import json
import smtplib
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import logging
from typing import Dict, Any, Optional

US_EQUITY_MARKET_TOKENS = {"US", "UN", "UW", "UQ", "UA", "UP", "UR", "UV"}
INVALID_EQUITY_SYMBOL_TOKENS = {"", "-", "N/A", "NA", "NONE", "NULL", "USD", "CASH", "US DOLLAR", "US"}

def normalize_us_equity_symbol(value: Any) -> Optional[str]:
    """Normalize issuer/Bloomberg-style US equity tickers to LongPort symbols."""
    text = re.sub(r"\s+", " ", str(value or "").strip().upper())
    if text in INVALID_EQUITY_SYMBOL_TOKENS:
        return None

    text = text.replace("/", ".")
    if text.startswith("US."):
        text = text[3:]

    if text.endswith(".US"):
        base = text[:-3]
        if " " not in base:
            return text
        # Repair already-imported values like "PM US.US".
        text = base

    parts = text.split(" ")
    if len(parts) == 2 and parts[1] in US_EQUITY_MARKET_TOKENS:
        text = parts[0]
    elif len(parts) == 3 and parts[1] in US_EQUITY_MARKET_TOKENS and parts[2] == "EQUITY":
        text = parts[0]
    elif len(parts) != 1:
        return None

    if text in INVALID_EQUITY_SYMBOL_TOKENS or not re.fullmatch(r"[A-Z0-9.]+", text):
        return None
    return text if text.endswith(".US") else f"{text}.US"

def read_json_file(file_path: str) -> Dict[str, Any]:
    """读取JSON文件"""
    if not os.path.exists(file_path):
        return {}
    with open(file_path, 'r', encoding='utf-8') as f:
        return json.load(f)

def sendmail(receiver_email: str, subject: str, body: str, Cc: str = None, mimeType: str = 'plain'):
    """发送邮件"""
    sender_email = "gongzi_quant@163.com"
    sender_password = "FVUNTBQWBQLJONRT"
    
    message = MIMEMultipart()
    message.attach(MIMEText(body, mimeType, 'utf-8'))
    message["From"] = sender_email
    message["To"] = receiver_email
    message["Cc"] = Cc
    message["Subject"] = subject
    
    try:
        with smtplib.SMTP_SSL("smtp.163.com") as server:
            server.connect("smtp.163.com", 465)
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, receiver_email, message.as_string())
            logging.info(f"send mail success {subject} {body}")
    except Exception as err:
        logging.error(err)

def send_alert_email(subject: str, body: str):
    """发送告警邮件"""
    receiver_email = "405290618@qq.com"
    try:
        body_str = str(body)
        if len(body_str) > 10000:
            body_str = body_str[:10000] + "\n...[内容已截断]"
        sendmail(receiver_email, subject, body_str)
    except Exception as e:
        logging.error(f"Failed to send alert email: {e}")

def mask_account_id(account_id: str) -> str:
    """脱敏显示账户ID"""
    if not account_id:
        return ""
    return f"***{account_id[-4:]}" if len(account_id) > 4 else account_id
