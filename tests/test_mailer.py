"""Ramas de envío SMTP (spec 014 §4).

Sin un relay real, se ejercitan ambos caminos con un `smtplib.SMTP` falso:
`use_tls=True` debe llamar `starttls()`; `use_tls=False` no. También se valida
que sin `smtp_host` se lanza RuntimeError.
"""
import smtplib

import pytest

import app.mailer as mailer


class _FakeSMTP:
    """Doble de smtplib.SMTP que registra las llamadas relevantes."""

    instances: list = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls: list = []
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, context=None):
        self.calls.append(("starttls", context is not None))

    def login(self, user, password):
        self.calls.append(("login", user))

    def sendmail(self, from_addr, to_addrs, msg):
        self.calls.append(("sendmail", from_addr, to_addrs))


@pytest.fixture(autouse=True)
def _reset_fake():
    _FakeSMTP.instances = []
    yield


def _config(**overrides):
    base = {
        "host": "smtp.test.local",
        "port": 587,
        "username": "",
        "password": "",
        "use_tls": True,
        "from_addr": "anomalia@test.local",
    }
    base.update(overrides)
    return base


def test_tls_path_calls_starttls(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    mailer._send_email_sync("dest@example.com", "S", "B", _config(use_tls=True), 5)
    calls = _FakeSMTP.instances[0].calls
    assert "ehlo" in calls
    assert any(isinstance(c, tuple) and c[0] == "starttls" for c in calls)
    assert any(isinstance(c, tuple) and c[0] == "sendmail" for c in calls)


def test_plain_path_skips_starttls(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    mailer._send_email_sync("dest@example.com", "S", "B", _config(use_tls=False), 5)
    calls = _FakeSMTP.instances[0].calls
    assert not any(isinstance(c, tuple) and c[0] == "starttls" for c in calls)
    assert any(isinstance(c, tuple) and c[0] == "sendmail" for c in calls)


def test_login_only_when_credentials(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    mailer._send_email_sync(
        "dest@example.com", "S", "B",
        _config(use_tls=False, username="u", password="p"), 5,
    )
    calls = _FakeSMTP.instances[0].calls
    assert ("login", "u") in calls


def test_missing_host_raises(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    with pytest.raises(RuntimeError):
        mailer._send_email_sync("d@e.com", "S", "B", _config(host=""), 5)
