"""
IMAP 邮件拉取模块（AstrBot 插件版）
通过 IMAP 协议从企业微信邮箱获取最新邮件
配置通过参数传入，不依赖全局 config
"""

import imaplib
import email
import json
import socket
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import Optional
from datetime import datetime, timedelta
from pathlib import Path

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover - 独立运行兜底
    import logging

    logger = logging.getLogger("imap_fetcher")

# IMAP 连接/操作超时（秒）。避免服务器不可达时扫描无限卡住，
# 导致「已有扫描在运行，跳过本次」反复出现。
IMAP_CONNECT_TIMEOUT = 15
IMAP_SOCKET_TIMEOUT = 30


class EmailInfo:
    """邮件信息"""

    def __init__(
        self,
        uid: int,
        subject: str = "",
        sender: str = "",
        recipients: str = "",
        date: str = "",
        body_text: str = "",
        is_read: bool = False,
    ):
        self.uid = uid
        self.subject = subject
        self.sender = sender
        self.recipients = recipients
        self.date = date
        self.body_text = body_text
        self.is_read = is_read

    @property
    def date_parsed(self) -> Optional[datetime]:
        try:
            return parsedate_to_datetime(self.date) if self.date else None
        except Exception:
            return None

    def __repr__(self):
        return f"EmailInfo(uid={self.uid}, subject='{self.subject[:30]}...')"


class IMAPFetcher:
    """企业微信邮箱 IMAP 客户端"""

    def __init__(self, host: str, port: int, address: str, auth_code: str):
        self.imap_host = host
        self.imap_port = port
        self.email_address = address
        self.auth_code = auth_code
        self._conn: Optional[imaplib.IMAP4_SSL] = None

    def connect(self):
        # timeout 参数（Python 3.9+）：TCP 连接阶段超时
        self._conn = imaplib.IMAP4_SSL(
            self.imap_host, self.imap_port, timeout=IMAP_CONNECT_TIMEOUT
        )
        # 连接后的所有 socket 操作（login/select/search/fetch）也加超时
        if self._conn.sock is not None:
            self._conn.sock.settimeout(IMAP_SOCKET_TIMEOUT)
        self._conn.login(self.email_address, self.auth_code)
        logger.info(
            f"邮箱智能整理: 已连接邮箱服务器 {self.imap_host}:{self.imap_port}"
        )

    def disconnect(self):
        if self._conn:
            try:
                self._conn.logout()
            except Exception:
                pass
            self._conn = None

    def get_latest_emails(
        self, since_days: int = 7, max_count: int = 50, scan_read: bool = False
    ) -> list[EmailInfo]:
        self.connect()
        try:
            status, _ = self._conn.select("INBOX", readonly=not scan_read)
            if status != "OK":
                self._conn.select("INBOX", readonly=False)

            since_date = (datetime.now() - timedelta(days=since_days)).strftime(
                "%d-%b-%Y"
            )
            logger.info(f"邮箱智能整理: 扫描范围 {since_date} 至今")

            status, msg_ids = self._conn.search(None, f'(SINCE "{since_date}")')
            if status != "OK" or not msg_ids[0]:
                logger.info("邮箱智能整理: 没有找到新邮件")
                return []

            ids = msg_ids[0].split()
            recent_ids = ids[-max_count:] if len(ids) > max_count else ids
            logger.info(
                f"邮箱智能整理: 扫描到 {len(recent_ids)} 封邮件 (最新 {max_count} 封)"
            )

            emails = []
            for msg_id in recent_ids:
                try:
                    email_info = self._parse_email(msg_id)
                    if email_info:
                        emails.append(email_info)
                except Exception as e:
                    logger.warning(f"邮箱智能整理: 解析邮件 {msg_id.decode()} 失败: {e}")
                    continue

            logger.info(f"邮箱智能整理: 成功解析 {len(emails)} 封有效邮件")
            return emails

        finally:
            self.disconnect()

    def _parse_email(self, msg_id: bytes) -> Optional[EmailInfo]:
        status, data = self._conn.fetch(msg_id, "(RFC822)")
        if status != "OK" or not data[0]:
            return None

        raw_email = data[0][1]
        msg = email.message_from_bytes(raw_email)

        subject = self._decode_header_str(msg.get("Subject", "(无主题)"))
        sender = self._decode_header_str(msg.get("From", ""))
        recipients = self._decode_header_str(msg.get("To", ""))
        date_str = msg.get("Date", "")
        body_text = self._extract_text(msg)

        return EmailInfo(
            uid=int(msg_id),
            subject=subject,
            sender=sender,
            recipients=recipients,
            date=date_str,
            body_text=body_text[:8000],
        )

    def _decode_header_str(self, header_value: str) -> str:
        if not header_value:
            return ""
        parts = []
        decoded = decode_header(header_value)
        for part, encoding in decoded:
            if isinstance(part, bytes):
                if encoding:
                    parts.append(part.decode(encoding, errors="replace"))
                else:
                    parts.append(part.decode("utf-8", errors="replace"))
            else:
                parts.append(part)
        return " ".join(parts)

    def _extract_text(self, msg) -> str:
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition", ""))
                if "attachment" in content_disposition:
                    continue
                if content_type == "text/plain":
                    try:
                        payload = part.get_payload(decode=True)
                        if payload:
                            charset = part.get_content_charset() or "utf-8"
                            body += payload.decode(charset, errors="replace")
                    except Exception:
                        pass
        else:
            try:
                payload = msg.get_payload(decode=True)
                if payload:
                    charset = msg.get_content_charset() or "utf-8"
                    body = payload.decode(charset, errors="replace")
            except Exception:
                pass
        return body.strip()[:10000]


class StateManager:
    """持久化状态管理 (跟踪已处理邮件)"""

    def __init__(self, data_dir: str = "./data"):
        self.state_file = Path(data_dir) / "state.json"
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict:
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {
                    "last_check_time": data.get("last_check_time"),
                    "processed_uids": set(data.get("processed_uids", [])),
                    "last_emails": data.get("last_emails", []),
                }
        except (FileNotFoundError, json.JSONDecodeError):
            return {
                "last_check_time": None,
                "processed_uids": set(),
                "last_emails": [],
            }

    def save(self, last_check_time: str, processed_uids: set[int], last_emails: list):
        data = {
            "last_check_time": last_check_time,
            "processed_uids": list(processed_uids),
            "last_emails": last_emails,
        }
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
