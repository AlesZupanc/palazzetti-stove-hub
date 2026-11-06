"""Outgoing e-mail (account requests, approvals, password resets).

Mails are queued and sent by a background thread so a slow SMTP server never
blocks a web request. Without an SMTP server configured, mails are written to
the log instead (journalctl -u stove-cloud), so approval links are never lost.
"""

import logging
import queue
import smtplib
import ssl
import threading
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

log = logging.getLogger("mail")


class Mailer:
    def __init__(self, cfg, on_error=None):
        self.host = cfg.smtp_host
        self.port = cfg.smtp_port
        self.security = cfg.smtp_security
        self.user = cfg.smtp_user
        self.password = cfg.smtp_password
        self.sender = cfg.mail_from
        self.on_error = on_error
        self._queue = queue.Queue(maxsize=200)
        self._thread = None

    @property
    def configured(self):
        return bool(self.host and self.sender)

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="mailer", daemon=True)
            self._thread.start()

    def send(self, to, subject, body):
        """Queue a plain-text mail (non-blocking)."""
        if not self.configured:
            log.warning("SMTP not configured - mail not sent.\nTo: %s\nSubject: %s\n\n%s", to, subject, body)
            return False
        self.start()
        try:
            self._queue.put_nowait((to, subject, body))
            return True
        except queue.Full:
            log.error("mail queue full, dropping mail to %s", to)
            return False

    def send_now(self, to, subject, body):
        """Send immediately; raises on failure, else returns the Message-ID (for "stove_cloud.py test-mail")."""
        if not self.configured:
            raise RuntimeError("SMTP is not configured ([mail] smtp_host / from_addr)")
        return self._deliver(to, subject, body)

    def _run(self):
        while True:
            to, subject, body = self._queue.get()
            for attempt in range(3):
                try:
                    self._deliver(to, subject, body)
                    log.info("mail sent to %s: %s", to, subject)
                    break
                except (OSError, smtplib.SMTPException) as e:
                    log.warning("sending mail to %s failed (attempt %d): %s", to, attempt + 1, e)
                    if attempt == 2:
                        log.error("giving up on mail to %s:\nSubject: %s\n\n%s", to, subject, body)
                        if self.on_error:
                            self.on_error(f"mail to {to} failed: {e}")
                    else:
                        threading.Event().wait(20 * (attempt + 1))

    def _deliver(self, to, subject, body):
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        domain = parseaddr(self.sender)[1].rpartition("@")[2] or None   # also for "Name <addr>"
        msg["Message-ID"] = make_msgid(domain=domain)
        msg["Auto-Submitted"] = "auto-generated"                         # RFC 3834: no auto-replies
        msg.set_content(body)
        context = ssl.create_default_context()
        if self.security == "ssl":
            server = smtplib.SMTP_SSL(self.host, self.port, timeout=30, context=context)
        else:
            server = smtplib.SMTP(self.host, self.port, timeout=30)
        try:
            server.ehlo()
            if self.security == "starttls":
                server.starttls(context=context)
                server.ehlo()
            if self.user:
                server.login(self.user, self.password)
            server.send_message(msg)
        finally:
            try:
                server.quit()
            except (OSError, smtplib.SMTPException):
                server.close()
        return msg["Message-ID"]
