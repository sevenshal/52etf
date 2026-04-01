import os
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import logging
from typing import Dict, Any, TypeVar, Type, Generic
from dataclasses import dataclass
from pathlib import Path
from pydantic import BaseModel

DATA_DIR = "/var/lib/quant_robot/account_data"

T = TypeVar('T')

def ensure_data_dir(account_id: str, sub_dir: str = "") -> str:
    """确保数据目录存在并返回目录路径"""
    account_dir = os.path.join(DATA_DIR, account_id, sub_dir)
    if not os.path.exists(account_dir):
        os.makedirs(account_dir)
    return account_dir

def get_data_file(account_id: str, filename: str, sub_dir: str = "") -> str:
    """获取数据文件的完整路径"""
    return os.path.join(ensure_data_dir(account_id, sub_dir), filename)

def read_json_file(file_path: str) -> Dict[str, Any]:
    """读取JSON文件"""
    if not os.path.exists(file_path):
        return {}
    with open(file_path, 'r', encoding='utf-8') as f:
        return json.load(f)

def write_json_file(file_path: str, data: Dict[str, Any]) -> None:
    """写入JSON文件"""
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

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
        if len(body_str) > 5000:
            body_str = body_str[:5000] + "\n...[内容已截断]"
        sendmail(receiver_email, subject, body_str)
    except Exception as e:
        logging.error(f"Failed to send alert email: {e}")

def load_config_file(account_id: str, file_name: str, config_class: Type[T]) -> T:
    """
    通用配置文件加载方法
    
    Args:
        account_id: 账户ID
        file_name: 配置文件名
        config_class: 配置类类型
        
    Returns:
        T: 配置类实例
    """
    try:
        file_path = get_data_file(account_id, file_name)
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"配置文件不存在: {file_path}")
            
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            
        # 检查是否是dataclass或BaseModel
        if hasattr(config_class, '__dataclass_fields__'):  # dataclass
            return config_class(**data)
        elif issubclass(config_class, BaseModel):  # Pydantic BaseModel
            return config_class(**data)
        else:
            raise ValueError(f"配置类 {config_class.__name__} 必须是dataclass或Pydantic BaseModel")
            
    except Exception as e:
        logging.error(f"加载配置文件失败 {file_name}: {str(e)}")
        raise

def save_config_file(account_id: str, file_name: str, config: Any) -> None:
    """
    通用配置文件保存方法
    
    Args:
        account_id: 账户ID
        file_name: 配置文件名
        config: 配置对象(必须可以转换为dict)
    """
    try:
        file_path = get_data_file(account_id, file_name)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        
        # 如果是Pydantic BaseModel，使用dict方法
        if isinstance(config, BaseModel):
            data = config.model_dump()
        # 如果是dataclass，使用__dict__
        elif hasattr(config, '__dataclass_fields__'):
            data = config.__dict__
        else:
            data = config
            
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            
    except Exception as e:
        logging.error(f"保存配置文件失败 {file_name}: {str(e)}")
        raise

def mask_account_id(account_id: str) -> str:
    """脱敏显示账户ID"""
    if not account_id:
        return ""
    return f"***{account_id[-4:]}" if len(account_id) > 4 else account_id
