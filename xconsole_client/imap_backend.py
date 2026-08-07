# -*- coding: utf-8 -*-
"""IMAP email backend for grok-build-auth.

Uses a local IMAP mailbox (e.g. Mailu/Dovecot) to receive x.ai verification
codes.  Designed for single-account, single-thread usage on a server.

Usage:
    from xconsole_client.imap_backend import ImapInbox
    inbox = ImapInbox(
        server="192.168.203.4",
        username="grok@weesai.com",
        password="...",
        email="grok@weesai.com",
    )
    address = inbox.create()           # returns the email address
    # ... trigger x.ai verification email ...
    code = inbox.wait_for_code(timeout=120)
"""
from __future__ import annotations

import email as email_lib
import imaplib
import re
import socket
import time
from typing import Optional

# ---------------------------------------------------------------------------
# Code extractor — matches x.ai verification code formats.
# ---------------------------------------------------------------------------
_CODE_PATTERNS = (
    # x.ai current format: "LSQ-OPU" (3 alphanum + dash + 3 alphanum = 7 chars)
    re.compile(r"(?<![A-Z0-9])([A-Z0-9]{3}-[A-Z0-9]{3})(?![A-Z0-9])"),
    # x.ai legacy format: 6 uppercase alphanumeric, no dash (e.g. "XAI0X1")
    re.compile(r"(?<![A-Z0-9])([A-Z0-9]{6})(?![A-Z0-9])"),
    # keyword-anchored fallbacks
    re.compile(
        r"(?i)(?:code|otp|验证码|verification|verify)\s*[:：]?\s*([A-Z0-9]{3}-[A-Z0-9]{3})"
    ),
    re.compile(
        r"(?i)(?:code|otp|验证码|verification|verify)\s*[:：]?\s*([A-Z0-9]{6})"
    ),
)


def extract_code(text: str) -> Optional[str]:
    """Extract an x.ai-style verification code from arbitrary text."""
    if not text:
        return None
    for pat in _CODE_PATTERNS:
        m = pat.search(text)
        if m:
            raw = m.group(1) if m.groups() else m.group(0)
            if raw.replace("-", "").isdigit():
                continue
            return raw.upper()
    return None


def _decode_header_value(value: str) -> str:
    """Decode RFC 2047 encoded header values."""
    result: list[str] = []
    for decoded_part, charset in email_lib.header.decode_header(value):
        if isinstance(decoded_part, bytes):
            try:
                result.append(decoded_part.decode(charset or "utf-8", errors="replace"))
            except (LookupError, UnicodeDecodeError):
                result.append(decoded_part.decode("utf-8", errors="replace"))
        else:
            result.append(str(decoded_part))
    return "".join(result)


class ImapInbox:
    """IMAP mailbox poller for x.ai verification codes."""

    def __init__(
        self,
        server: str,
        username: str,
        password: str,
        email: str,
        *,
        timeout: float = 120.0,
        interval: float = 3.0,
        debug: bool = False,
        use_ssl: bool = False,
        port: int | None = None,
    ):
        self.server = server
        self.username = username
        self.password = password
        self.email = email
        self.timeout = timeout
        self.interval = interval
        self.debug = debug
        self.use_ssl = use_ssl
        self.port = port or (993 if use_ssl else 143)
        self._conn: imaplib.IMAP4 | None = None
        self._created = False

    def _connect(self) -> imaplib.IMAP4:
        if self._conn is not None:
            return self._conn
        socket.setdefaulttimeout(15)
        if self.use_ssl:
            conn = imaplib.IMAP4_SSL(self.server, self.port)
        else:
            conn = imaplib.IMAP4(self.server, self.port)
        conn.login(self.username, self.password)
        self._conn = conn
        if self.debug:
            print(f"  [IMAP] connected to {self.server}:{self.port}")
        return conn

    def create(self) -> str:
        """Return the email address to use for registration."""
        self._created = True
        if self.debug:
            print(f"  [IMAP] using mailbox: {self.email}")
        return self.email

    def close(self):
        """Close the IMAP connection."""
        if self._conn is not None:
            try:
                self._conn.logout()
            except Exception:
                pass
            self._conn = None

    def wait_for_code(self, timeout: Optional[float] = None) -> str:
        """Poll the INBOX until an x.ai verification code appears.

        Returns the code string.  Raises TimeoutError if nothing arrives.
        """
        if not self._created:
            raise RuntimeError("Call create() first")
        deadline = time.time() + (timeout or self.timeout)
        conn = self._connect()
        seen_uids: set[str] = set()

        while True:
            try:
                conn.select("INBOX", readonly=True)
                # Search for all messages
                status, data = conn.uid("SEARCH", None, "ALL")
                if status != "OK":
                    if self.debug:
                        print(f"  [IMAP] SEARCH failed: {status}")
                    if time.time() >= deadline:
                        raise TimeoutError(
                            f"IMAP: no x.ai code for {self.email} within "
                            f"{timeout or self.timeout:.0f}s"
                        )
                    time.sleep(self.interval)
                    continue

                uids = data[0].split() if data[0] else []
                new_uids = [uid.decode() for uid in uids if uid.decode() not in seen_uids]

                for uid in new_uids:
                    seen_uids.add(uid)
                    status, msg_data = conn.uid("FETCH", uid, "(BODY.PEEK[] FLAGS)")
                    if status != "OK" or not msg_data or msg_data[0] is None:
                        continue

                    raw = _get_email_body(msg_data)
                    if not raw:
                        continue

                    msg = email_lib.message_from_bytes(raw)
                    body = _get_body_text(msg)
                    subject = _decode_header_value(msg.get("Subject", ""))
                    from_addr = _decode_header_value(msg.get("From", ""))

                    combined = f"{subject} {body} {from_addr}"
                    code = extract_code(combined)
                    if code:
                        if self.debug:
                            print(
                                f"  [IMAP] code found: {code} "
                                f"(from: {from_addr}, subject: {subject})"
                            )
                        return code

                if self.debug:
                    print(
                        f"  [IMAP] polling... ({len(seen_uids)} emails seen, "
                        f"{len(new_uids)} new)"
                    )

            except (imaplib.IMAP4.error, OSError, ConnectionError) as e:
                if self.debug:
                    print(f"  [IMAP] connection error: {e}, reconnecting...")
                self._conn = None
                try:
                    conn = self._connect()
                except Exception:
                    if time.time() >= deadline:
                        raise TimeoutError(
                            f"IMAP: no x.ai code for {self.email} within "
                            f"{timeout or self.timeout:.0f}s (connection lost)"
                        )
                    time.sleep(self.interval)
                    continue

            if time.time() >= deadline:
                raise TimeoutError(
                    f"IMAP: no x.ai code for {self.email} within "
                    f"{timeout or self.timeout:.0f}s ({len(seen_uids)} emails seen)"
                )

            time.sleep(self.interval)


def _get_email_body(msg_data: list) -> bytes | None:
    """Extract raw email bytes from an IMAP FETCH response."""
    if not msg_data:
        return None
    # msg_data[0] could be a tuple (header, body) or bytes
    item = msg_data[0]
    if isinstance(item, tuple):
        # item is (header_flags, body_bytes)
        raw = item[1]
        if isinstance(raw, bytes):
            return raw
    elif isinstance(item, bytes):
        return item
    return None


def _get_body_text(msg: email_lib.message.Message) -> str:
    """Extract plain text body from an email Message."""
    parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            cdisp = str(part.get("Content-Disposition", ""))
            if ctype == "text/plain" and "attachment" not in cdisp:
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        parts.append(payload.decode(charset, errors="replace"))
                except Exception:
                    pass
    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                parts.append(payload.decode(charset, errors="replace"))
        except Exception:
            pass
    return "\n".join(parts)
