"""Streams messages out of a .pst/.ost file with libpff (`pip install libpff-python`, imported as
`pypff`). Only plain data crosses this module's boundary; pypff objects never leave it."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from email.parser import HeaderParser
from email.utils import getaddresses, parseaddr
from typing import Iterator

from .extractors import decode_bytes, html_to_text, rtf_to_text

# MAPI property tags
PR_SENDER_NAME = 0x0C1A
PR_SENDER_EMAIL_ADDRESS = 0x0C1F
PR_SENDER_SMTP_ADDRESS = 0x5D01
PR_SENT_REPRESENTING_EMAIL = 0x0065
PR_INTERNET_MESSAGE_ID = 0x1035
PR_MESSAGE_CODEPAGE = 0x3FFD
PR_INTERNET_CPID = 0x3FDE
PR_DISPLAY_NAME = 0x3001
PR_EMAIL_ADDRESS = 0x3003
PR_SMTP_ADDRESS = 0x39FE
PR_RECIPIENT_TYPE = 0x0C15
PR_ATTACH_FILENAME = 0x3704
PR_ATTACH_LONG_FILENAME = 0x3707
PR_ATTACH_MIME_TAG = 0x370E
PR_DISPLAY_NAME_ATTACH = 0x3001

STRING_TYPES = (0x001F, 0x001E)
INT_TYPES = (0x0002, 0x0003, 0x0014)
RECIPIENT_KIND = {1: "to", 2: "cc", 3: "bcc"}


@dataclass
class ParsedAttachment:
    filename: str
    mime_type: str | None
    size: int
    data: bytes | None  # None when over the size cap or unreadable
    note: str = ""


@dataclass
class ParsedEmail:
    folder_path: str
    subject: str
    sender_name: str
    sender_email: str
    recipients: list[dict]
    sent_at: dt.datetime | None
    internet_message_id: str | None
    headers: str
    body_text: str
    body_html: str
    attachments: list[ParsedAttachment] = field(default_factory=list)


def _record_props(item, record_set: int = 0) -> dict[int, object]:
    props: dict[int, object] = {}
    try:
        if item.number_of_record_sets <= record_set:
            return props
        rs = item.get_record_set(record_set)
        for i in range(rs.number_of_entries):
            e = rs.get_entry(i)
            try:
                if e.value_type in STRING_TYPES:
                    props[e.entry_type] = e.data_as_string
                elif e.value_type in INT_TYPES:
                    props[e.entry_type] = e.data_as_integer
            except Exception:
                continue
    except Exception:
        pass
    return props


def _clean_addr(value: object) -> str:
    s = (str(value) if value else "").strip()
    # Exchange X.500 DNs ("/O=ENRON/OU=NA/CN=...") are not useful addresses
    if s.upper().startswith("/O=") or "@" not in s:
        return ""
    return s


def _to_utc(value) -> dt.datetime | None:
    if not isinstance(value, dt.datetime) or value.year < 1971:
        return None
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value


def _recipients(msg, headers) -> list[dict]:
    out: list[dict] = []
    try:
        recips = msg.recipients
        n = recips.number_of_record_sets if recips is not None else 0
        for i in range(n):
            p = _record_props(recips, i)
            email = _clean_addr(p.get(PR_SMTP_ADDRESS)) or _clean_addr(p.get(PR_EMAIL_ADDRESS))
            name = str(p.get(PR_DISPLAY_NAME) or "")
            if email or name:
                out.append({"name": name, "email": email, "type": RECIPIENT_KIND.get(p.get(PR_RECIPIENT_TYPE), "to")})
    except Exception:
        pass
    if not out and headers is not None:
        for kind in ("to", "cc", "bcc"):
            for name, email in getaddresses(headers.get_all(kind, [])):
                if email or name:
                    out.append({"name": name, "email": email, "type": kind})
    return out


def _body(msg, codepage) -> tuple[str, str]:
    html = ""
    try:
        raw_html = msg.html_body
        if raw_html:
            html = decode_bytes(raw_html, codepage)
    except Exception:
        pass
    text = ""
    try:
        raw = msg.plain_text_body
        if raw:
            text = decode_bytes(raw, codepage)
    except Exception:
        pass
    if not text.strip() and html:
        text = html_to_text(html)
    if not text.strip():
        try:
            rtf = msg.rtf_body
            if rtf:
                text = rtf_to_text(decode_bytes(rtf, codepage))
        except Exception:
            pass
    return text, html


def _attachments(msg, max_bytes: int) -> list[ParsedAttachment]:
    out: list[ParsedAttachment] = []
    for i in range(msg.number_of_attachments):
        try:
            a = msg.get_attachment(i)
            p = _record_props(a)
            name = (
                getattr(a, "long_filename", None)
                or p.get(PR_ATTACH_LONG_FILENAME)
                or p.get(PR_ATTACH_FILENAME)
                or p.get(PR_DISPLAY_NAME_ATTACH)
                or f"attachment-{i + 1}"
            )
            mime = p.get(PR_ATTACH_MIME_TAG) or None
            size = a.size or 0
            data, note = None, ""
            if size == 0:
                note = "embedded item or empty"
            elif size > max_bytes:
                note = f"skipped: larger than {max_bytes // (1024 * 1024)} MB"
            else:
                a.seek_offset(0)
                data = a.read_buffer(size)
            out.append(ParsedAttachment(str(name), str(mime) if mime else None, size, data, note))
        except Exception as exc:
            out.append(ParsedAttachment(f"attachment-{i + 1}", None, 0, None, f"error: {exc}"[:200]))
    return out


def parse_message(msg, folder_path: str, max_attachment_bytes: int) -> ParsedEmail:
    p = _record_props(msg)
    codepage = p.get(PR_INTERNET_CPID) or p.get(PR_MESSAGE_CODEPAGE)
    raw_headers = ""
    try:
        raw_headers = msg.transport_headers or ""
    except Exception:
        pass
    headers = HeaderParser().parsestr(raw_headers) if raw_headers else None

    sender_name = str(msg.sender_name or p.get(PR_SENDER_NAME) or "")
    sender_email = (
        _clean_addr(p.get(PR_SENDER_SMTP_ADDRESS))
        or _clean_addr(p.get(PR_SENDER_EMAIL_ADDRESS))
        or _clean_addr(p.get(PR_SENT_REPRESENTING_EMAIL))
    )
    if headers is not None and (not sender_email or not sender_name):
        h_name, h_email = parseaddr(headers.get("from", ""))
        sender_email = sender_email or h_email
        sender_name = sender_name or h_name

    message_id = p.get(PR_INTERNET_MESSAGE_ID) or (headers.get("message-id") if headers is not None else None)
    sent_at = None
    for attr in ("client_submit_time", "delivery_time", "creation_time"):
        try:
            sent_at = _to_utc(getattr(msg, attr))
        except Exception:
            sent_at = None
        if sent_at:
            break

    text, html = _body(msg, codepage)
    return ParsedEmail(
        folder_path=folder_path,
        subject=str(msg.subject or "").replace("\x01", "").strip(),
        sender_name=sender_name.strip(),
        sender_email=sender_email.strip(),
        recipients=_recipients(msg, headers),
        sent_at=sent_at,
        internet_message_id=str(message_id).strip() if message_id else None,
        headers=raw_headers,
        body_text=text,
        body_html=html,
        attachments=_attachments(msg, max_attachment_bytes),
    )


def count_items(pst) -> int:
    def walk(folder) -> int:
        n = folder.number_of_sub_messages
        for i in range(folder.number_of_sub_folders):
            n += walk(folder.get_sub_folder(i))
        return n

    return walk(pst.get_root_folder())


def iter_messages(path: str, max_attachment_bytes: int) -> Iterator[tuple[ParsedEmail | None, str | None]]:
    """Yields (email, None) per mail item and (None, reason) per skipped item."""
    import pypff

    pst = pypff.file()
    pst.open(path)
    try:
        yield from _walk(pst.get_root_folder(), "", pypff, max_attachment_bytes)
    finally:
        pst.close()


def open_and_count(path: str) -> int:
    import pypff

    pst = pypff.file()
    pst.open(path)
    try:
        return count_items(pst)
    finally:
        pst.close()


def _walk(folder, parent: str, pypff, max_bytes: int):
    name = folder.name or ""
    path = f"{parent}/{name}" if name else parent
    for i in range(folder.number_of_sub_messages):
        try:
            item = folder.get_sub_message(i)
        except Exception as exc:
            yield None, f"unreadable item: {exc}"
            continue
        if not isinstance(item, pypff.message):
            # calendar entries, contacts, distribution lists, etc.
            yield None, "non-mail item"
            continue
        try:
            yield parse_message(item, path or "/", max_bytes), None
        except Exception as exc:
            yield None, f"parse error: {exc}"
    for i in range(folder.number_of_sub_folders):
        try:
            sub = folder.get_sub_folder(i)
        except Exception:
            continue
        yield from _walk(sub, path, pypff, max_bytes)
