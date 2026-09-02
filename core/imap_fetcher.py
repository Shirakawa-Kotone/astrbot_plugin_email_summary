"""
IMAP 邮件拉取模块（AstrBot 插件版）
通过 IMAP 协议从企业微信邮箱获取最新邮件
配置通过参数传入，不依赖全局 config

v1.4.0 起支持：
- 附件提取：解析邮件内嵌附件并保存到本地 attachment_dir
  （附件元信息如文件名/大小/路径随 EmailInfo 返回）
- 编码容错：header/正文遇到 unknown-8bit 等未知编码时回退 UTF-8/GBK，
  避免单封邮件编码问题导致整封解析失败
"""

import imaplib
import email
import json
import re
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

# 单个附件最大体积（字节），超过则跳过不保存/不发送（QQ 文件也有大小限制）
MAX_ATTACHMENT_SIZE = 30 * 1024 * 1024  # 30MB

# 未知/异常编码回退顺序（unknown-8bit 常见于中文邮件但 Python 不认识）
_FALLBACK_ENCODINGS = ("utf-8", "gbk", "gb2312", "latin-1")


def _safe_decode(data: bytes, encoding: Optional[str]) -> str:
    """解码字节串。

    策略：声明编码（latin-1 类除外）→ UTF-8 → GBK → GB2312 → 声明编码（latin-1 类）→ latin-1，
    全部严格解码，成功即返回；全部失败再以 replace 兜底。
    这样能正确处理：
    - unknown-8bit 等 Python 不认识的占位编码（跳过声明，直接试 UTF-8/GBK）
    - 声明 utf-8 实为 gbk（UTF-8 严格解码失败后落到 GBK）
    - 声明 latin-1 实为 utf-8（latin-1 排到 UTF-8 之后，避免经典乱码）
    """
    if not data:
        return ""
    declared = ""
    if encoding:
        enc = encoding.strip().lower()
        if enc not in ("unknown", "unknown-8bit", "default", "ascii", "none", ""):
            declared = enc

    latinish = declared.startswith(("iso-8859", "latin", "windows-1252"))
    ordered: list[str] = []

    def _add(enc: str) -> None:
        if enc and enc not in ordered:
            ordered.append(enc)

    if declared and not latinish:
        _add(declared)
    _add("utf-8")
    _add("gbk")
    _add("gb2312")
    if declared and latinish:
        _add(declared)
    _add("latin-1")

    # 先严格解码
    for enc in ordered:
        try:
            return data.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    # 全部失败，replace 兜底
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return data.decode(enc, errors="replace")
        except LookupError:
            continue
    return data.decode("utf-8", errors="replace")


def _sanitize_filename(name: str) -> str:
    """清理文件名：去掉路径成分和非法字符，防止路径穿越。"""
    name = Path(name or "attachment").name
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip()
    name = name.strip(". ")
    return name or "attachment"


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f}KB"
    return f"{size / 1024 / 1024:.1f}MB"


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
        attachments: Optional[list[dict]] = None,
    ):
        self.uid = uid
        self.subject = subject
        self.sender = sender
        self.recipients = recipients
        self.date = date
        self.body_text = body_text
        self.is_read = is_read
        # attachments: [{"filename": str, "path": str, "size": int}, ...]
        self.attachments: list[dict] = attachments or []

    @property
    def has_attachment(self) -> bool:
        return bool(self.attachments)

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

    def __init__(
        self,
        host: str,
        port: int,
        address: str,
        auth_code: str,
        attachment_dir: str = "",
    ):
        self.imap_host = host
        self.imap_port = port
        self.email_address = address
        self.auth_code = auth_code
        # 附件保存根目录（可选）。留空则不提取附件。
        self.attachment_dir = attachment_dir
        if self.attachment_dir:
            Path(self.attachment_dir).mkdir(parents=True, exist_ok=True)
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
            attachment_count = 0
            for msg_id in recent_ids:
                try:
                    email_info = self._parse_email(msg_id)
                    if email_info:
                        emails.append(email_info)
                        attachment_count += len(email_info.attachments)
                except Exception as e:
                    logger.warning(f"邮箱智能整理: 解析邮件 {msg_id.decode()} 失败: {e}")
                    continue

            if attachment_count:
                logger.info(
                    f"邮箱智能整理: 成功解析 {len(emails)} 封有效邮件，"
                    f"提取附件 {attachment_count} 个（目录: {self.attachment_dir}）"
                )
            else:
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
        attachments = self._extract_attachments(msg, int(msg_id))

        return EmailInfo(
            uid=int(msg_id),
            subject=subject,
            sender=sender,
            recipients=recipients,
            date=date_str,
            body_text=body_text[:8000],
            attachments=attachments,
        )

    def _decode_header_str(self, header_value: str) -> str:
        if not header_value:
            return ""
        parts = []
        decoded = decode_header(header_value)
        for part, encoding in decoded:
            if isinstance(part, bytes):
                parts.append(_safe_decode(part, encoding))
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
                            charset = part.get_content_charset()
                            body += _safe_decode(payload, charset)
                    except Exception:
                        pass
        else:
            try:
                payload = msg.get_payload(decode=True)
                if payload:
                    charset = msg.get_content_charset()
                    body = _safe_decode(payload, charset)
            except Exception:
                pass
        return body.strip()[:10000]

    def _extract_attachments(self, msg, uid: int) -> list[dict]:
        """提取附件并保存到 <attachment_dir>/<uid>/，返回元信息列表。

        只提取「明确声明为 attachment」或「非内嵌图片/HTML 的带文件名部件」，
        避免把邮件签名里的 logo、内嵌图片当成附件。
        """
        if not self.attachment_dir or not msg.is_multipart():
            return []

        saved_dir = Path(self.attachment_dir) / str(uid)
        saved_dir.mkdir(parents=True, exist_ok=True)

        results: list[dict] = []
        seen_names: set[str] = set()
        for i, part in enumerate(msg.walk()):
            content_disposition = str(part.get("Content-Disposition", ""))
            filename = part.get_filename()
            if not filename:
                continue

            content_type = part.get_content_type()
            is_explicit_attachment = "attachment" in content_disposition
            is_inline_media = content_type.startswith(("image/", "text/html"))
            # 明确附件 或（有文件名且不是内嵌媒体）
            if not (is_explicit_attachment or not is_inline_media):
                continue

            try:
                payload = part.get_payload(decode=True)
            except Exception:
                continue
            if not payload:
                continue
            if len(payload) > MAX_ATTACHMENT_SIZE:
                logger.warning(
                    f"邮箱智能整理: 附件 {filename} 超过 {MAX_ATTACHMENT_SIZE // 1024 // 1024}MB，已跳过"
                )
                continue

            clean_name = _sanitize_filename(self._decode_header_str(filename))
            if clean_name in seen_names:
                stem, ext = Path(clean_name).stem, Path(clean_name).suffix
                clean_name = f"{stem}_{i}{ext}"
            seen_names.add(clean_name)

            try:
                file_path = saved_dir / clean_name
                with open(file_path, "wb") as f:
                    f.write(payload)
            except Exception as e:
                logger.warning(f"邮箱智能整理: 保存附件 {clean_name} 失败: {e}")
                continue

            results.append(
                {
                    "filename": clean_name,
                    "path": str(file_path),
                    "size": len(payload),
                    "size_str": _format_size(len(payload)),
                }
            )
        return results


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
