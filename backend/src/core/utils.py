import os
import json
import smtplib
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import logging
from typing import Dict, Any, Optional, List

US_EQUITY_MARKET_TOKENS = {"US", "UN", "UW", "UQ", "UA", "UP", "UR", "UV"}
INVALID_EQUITY_SYMBOL_TOKENS = {"", "-", "N/A", "NA", "NONE", "NULL", "USD", "CASH", "US DOLLAR", "US"}
EMAIL_ADDRESS_PATTERN = re.compile(r"^[^@\s,;，；]+@[^@\s,;，；]+\.[^@\s,;，；]+$")

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

def _normalize_email_recipients(value: Optional[str]) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    parts = [
        item.strip()
        for item in re.split(r"[,;，；\s]+", text)
        if item.strip()
    ]
    normalized = []
    seen = set()
    for item in parts:
        if not EMAIL_ADDRESS_PATTERN.fullmatch(item):
            raise ValueError(f"邮箱格式无效: {item}")
        dedupe_key = item.lower()
        if dedupe_key not in seen:
            normalized.append(item)
            seen.add(dedupe_key)
    return ", ".join(normalized) if normalized else None

def _split_email_recipients(value: Optional[str]) -> List[str]:
    normalized = _normalize_email_recipients(value)
    if not normalized:
        return []
    return [item.strip() for item in normalized.split(",") if item.strip()]

def sendmail(receiver_email: str, subject: str, body: str, Cc: str = None, mimeType: str = 'plain') -> bool:
    """发送邮件"""
    to_recipients = _split_email_recipients(receiver_email)
    cc_recipients = _split_email_recipients(Cc)
    all_recipients = to_recipients + cc_recipients
    if not all_recipients:
        logging.info("skip mail because recipient is empty: %s", subject)
        return False

    sender_email = "gongzi_quant@163.com"
    sender_password = "FVUNTBQWBQLJONRT"
    
    message = MIMEMultipart()
    message.attach(MIMEText(body, mimeType, 'utf-8'))
    message["From"] = sender_email
    message["To"] = ", ".join(to_recipients)
    if cc_recipients:
        message["Cc"] = ", ".join(cc_recipients)
    message["Subject"] = subject
    
    try:
        with smtplib.SMTP_SSL("smtp.163.com") as server:
            server.connect("smtp.163.com", 465)
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, all_recipients, message.as_string())
            logging.info("send mail success: %s -> %s", subject, ", ".join(all_recipients))
            return True
    except Exception as err:
        logging.error(err)
        return False

def send_configured_email(
    scenario_key: str,
    subject: str,
    body: str,
    Cc: str = None,
    mimeType: str = 'plain',
    receiver_email: Optional[str] = None,
) -> bool:
    """按邮件场景发送邮件；未配置场景时回退默认邮箱。"""
    try:
        resolved_receiver = _normalize_email_recipients(receiver_email)
        if not resolved_receiver:
            from .services.email_settings import resolve_email_recipient

            resolved_receiver = resolve_email_recipient(scenario_key)
        if not resolved_receiver:
            logging.info("skip mail because no recipient configured for scenario %s: %s", scenario_key, subject)
            return False
        return sendmail(resolved_receiver, subject, body, Cc=Cc, mimeType=mimeType)
    except Exception as e:
        logging.error("Failed to send configured email for scenario %s: %s", scenario_key, e)
        return False

def send_alert_email(subject: str, body: str, scenario_key: str = "system_alert") -> bool:
    """发送告警邮件"""
    try:
        body_str = str(body)
        if len(body_str) > 10000:
            body_str = body_str[:10000] + "\n...[内容已截断]"
        return send_configured_email(scenario_key, subject, body_str)
    except Exception as e:
        logging.error(f"Failed to send alert email: {e}")
        return False


def send_system_startup_email() -> bool:
    """发送系统启动通知（按 system_startup 场景收件人，未配置则回退默认邮箱）。"""
    import socket
    from datetime import datetime
    from zoneinfo import ZoneInfo

    env = os.getenv("ENV", "dev")
    started_at = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    robot_disabled = os.getenv("QUANT_ROBOT_DISABLED", "0") == "1"
    body = (
        "52etf 后端服务已启动。\n\n"
        f"启动时间: {started_at} (Asia/Shanghai)\n"
        f"环境: {env}\n"
        f"主机: {socket.gethostname()}\n"
        f"进程 PID: {os.getpid()}\n"
        f"自动交易机器人: {'已禁用 (QUANT_ROBOT_DISABLED=1)' if robot_disabled else '已启用'}\n"
    )
    return send_configured_email(
        "system_startup",
        f"52etf 后端服务启动通知 [{env}]",
        body,
    )

def mask_account_id(account_id: str) -> str:
    """脱敏显示账户ID"""
    if not account_id:
        return ""
    return f"***{account_id[-4:]}" if len(account_id) > 4 else account_id
