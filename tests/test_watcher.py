"""Offline tests: python -m pytest -q"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import salto     # noqa: E402
import watcher   # noqa: E402

FIX = ROOT / "tests" / "fixtures"
TODAY = datetime(2026, 11, 4, tzinfo=timezone.utc)
REQ = yaml.safe_load((ROOT / "config.yaml").read_text())["salto"]


def detail(name):
    return salto.parse_detail((FIX / f"salto_detail_{name}.html").read_text(), f"https://example/{name}")


# ---------------------------------------------------------------- parser

def test_parse_ngo_training():
    t = detail("ictsm")
    assert t["title"].startswith("ICT&SM 4.0")
    assert t["type"] == "Training Course"
    assert (t["dates"], t["place"]) == ("12-20 February 2027", "Hemmersheim, Germany")
    assert t["deadline"] == "29 December 2026"
    assert t["selection"] == "12 January 2027"
    assert t["participants"] == "35"
    assert "Germany" in t["from"]
    assert t["language"] == "English"
    assert t["organiser"].startswith("Brezn")
    assert t["fee"] == "No"
    assert "Germany 285€" in t["travel"]


def test_parse_na_forum_with_icon_tooltips():
    t = detail("forum")
    assert t["type"].startswith("Conference")
    assert t["deadline"] == "11 November 2026"
    assert "Erasmus+ Youth Programme countries" in t["from"]
    assert "Germany" in t["from"]          # read from the icon's title attribute
    assert "get in touch with the NA" in t["fee"]


def test_navigation_words_do_not_count():
    t = detail("ictsm")
    assert "SALTO Digital Youth Work" not in t["core_text"]
    assert "footer" not in t["core_text"].lower()


# ---------------------------------------------------------------- requirements

def test_matches_and_cost_flags_ngo():
    t = detail("ictsm")
    ok, info = salto.evaluate(t, REQ, TODAY)
    assert ok and "digital" in info["topics"] and "ICT" in info["topics"]
    assert info["eligibility"].startswith("✅")
    assert info["days_left"] == 55
    flags = " ".join(salto.cost_flags(t))
    assert "No participation fee" in flags
    assert "Accommodation & food covered" in flags
    assert "Germany: up to 285 €" in flags and "green travel" in flags


def test_matches_and_cost_flags_na():
    t = detail("forum")
    ok, info = salto.evaluate(t, REQ, TODAY)
    assert ok and "AI" in info["topics"]
    flags = " ".join(salto.cost_flags(t))
    assert "ask your National Agency" in flags and "JUGEND für Europa" in flags


def test_rejects_not_open_to_germany():
    ok, info = salto.evaluate(detail("notopen"), REQ, TODAY)
    assert not ok and "Germany" in info["reason"]


def test_rejects_passed_deadline():
    ok, info = salto.evaluate(detail("forum"), REQ, datetime(2026, 11, 20, tzinfo=timezone.utc))
    assert not ok and info["reason"] == "deadline passed"


def test_activity_type_filter():
    req = dict(REQ, activity_types=["Training Course"])
    assert salto.evaluate(detail("ictsm"), req, TODAY)[0]
    assert not salto.evaluate(detail("forum"), req, TODAY)[0]


def test_whole_word_keywords():
    assert watcher.term_found("New BIP on robotics", "BIP")
    assert not watcher.term_found("bipolar research", "BIP")
    assert watcher.term_found("Modern C++ workshop", "C++")
    assert not watcher.term_found("said", "AI")


# ---------------------------------------------------------------- end to end

class FakeResp:
    def __init__(self, text, url):
        self.text, self.url, self.content = text, url, text.encode()


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    pages = {
        "b_offset=0&": FIX / "salto_browse_p1.html",
        "ictsm": FIX / "salto_detail_ictsm.html",
        "youth-work-digital": FIX / "salto_detail_forum.html",
        "gardening": FIX / "salto_detail_notopen.html",
        "featured-thing": FIX / "salto_detail_notopen.html",
    }

    def fake_fetch(url):
        for key, path in pages.items():
            if key in url:
                return FakeResp(path.read_text(), url)
        if "b_offset=" in url:
            return FakeResp((FIX / "salto_browse_end.html").read_text(), url)
        raise ConnectionError(f"offline test: {url}")

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    cfg["web_search"]["enabled"] = False
    cfg["sources"] = []
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))
    sent = []
    monkeypatch.setattr(watcher, "CONFIG_FILE", tmp_path / "config.yaml")
    monkeypatch.setattr(watcher, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(watcher, "fetch", fake_fetch)
    monkeypatch.setattr(watcher, "send_email", lambda s, t, h: sent.append((s, t, h)))
    monkeypatch.setattr(salto.time, "sleep", lambda s: None)
    monkeypatch.setattr(salto, "_today", lambda: TODAY)
    monkeypatch.setattr(watcher.time, "sleep", lambda s: None)
    monkeypatch.setenv("SMTP_USER", "me@example.com")
    monkeypatch.setenv("SMTP_PASS", "x")
    return tmp_path, sent, pages


def test_first_run_then_only_new(sandbox, monkeypatch):
    tmp, sent, pages = sandbox
    watcher.run(dry_run=False)
    assert len(sent) == 1
    subject, text, html_body = sent[0]
    assert "is live" in subject
    assert "ICT&SM 4.0" in text and "Gardening" not in text
    assert "Apply by" in html_body
    seen = json.loads((tmp / "state.json").read_text())["sources"][watcher.SALTO_NAME]["seen"]
    assert {"15309", "15500"} <= set(seen)

    watcher.run(dry_run=False)          # nothing new -> no email
    assert len(sent) == 1

    # a new matching training appears on the listing
    new_listing = (FIX / "salto_browse_p1.html").read_text().replace(
        "</body>", '<a href="/tools/european-training-calendar/training/ai-for-youth-workers.15600/">x</a></body>')
    (tmp / "p1.html").write_text(new_listing)
    pages["b_offset=0&"] = tmp / "p1.html"
    pages["ai-for-youth"] = FIX / "salto_detail_forum.html"
    watcher.run(dry_run=False)
    assert len(sent) == 2 and sent[1][0].startswith("New: 1 new SALTO training")


def test_passing_mention_is_not_enough():
    t = detail("ictsm")
    t = dict(t, title="Outdoor Leadership Course",
             description="A course on outdoor leadership for youth workers. Participants also use digital tools "
                         "to document their hikes and reflect together in the evenings.",
             recommended="Youth workers")
    ok, info = salto.evaluate(t, REQ, TODAY)
    assert not ok and "in passing" in info["reason"]
    t["title"] = "Outdoor Leadership with Digital Storytelling"
    assert salto.evaluate(t, REQ, TODAY)[0]


def test_skips_quietly_until_email_is_set_up(sandbox, monkeypatch):
    tmp, sent, _ = sandbox
    monkeypatch.delenv("SMTP_PASS")
    assert watcher.run(dry_run=False) == 0
    assert not sent and not (tmp / "state.json").exists()


# ---------------------------------------------------------------- email sending

class _Conn:
    """Fake SMTP connection for testing send_email without a network."""
    log = []
    def __init__(self, behaviour, port): self.behaviour, self.port = behaviour, port
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def starttls(self): _Conn.log.append(f"starttls:{self.port}")
    def login(self, u, p):
        _Conn.log.append(f"login:{self.port}:{u}:{p}")
        if self.behaviour.get(self.port) == "disconnect":
            raise watcher.smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
        if self.behaviour.get(self.port) == "badpass":
            raise watcher.smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")
    def send_message(self, m): _Conn.log.append(f"sent:{self.port}")


def _fake_smtp(monkeypatch, behaviour):
    _Conn.log = []
    monkeypatch.setattr(watcher.smtplib, "SMTP_SSL", lambda h, p, timeout: _Conn(behaviour, p))
    monkeypatch.setattr(watcher.smtplib, "SMTP", lambda h, p, timeout: _Conn(behaviour, p))
    monkeypatch.delenv("SMTP_PORT", raising=False)


def test_email_cleans_pasted_secrets(monkeypatch):
    _fake_smtp(monkeypatch, {})
    monkeypatch.setenv("SMTP_USER", " me@gmail.com\n")
    monkeypatch.setenv("SMTP_PASS", "abcd efgh\u00a0ijkl\u200bmnop\r\n")
    watcher.send_email("s", "t", "<p>h</p>")
    assert "login:465:me@gmail.com:abcdefghijklmnop" in _Conn.log and "sent:465" in _Conn.log


def test_email_falls_back_to_port_587_when_465_disconnects(monkeypatch):
    _fake_smtp(monkeypatch, {465: "disconnect"})
    monkeypatch.setenv("SMTP_USER", "me@gmail.com")
    monkeypatch.setenv("SMTP_PASS", "abcdefghijklmnop")
    watcher.send_email("s", "t", "<p>h</p>")
    assert _Conn.log[-1] == "sent:587" and "starttls:587" in _Conn.log


def test_email_wrong_password_gives_clear_message(monkeypatch):
    _fake_smtp(monkeypatch, {465: "badpass"})
    monkeypatch.setenv("SMTP_USER", "me@gmail.com")
    monkeypatch.setenv("SMTP_PASS", "wrongwrongwrong1")
    with pytest.raises(SystemExit, match="App Password"):
        watcher.send_email("s", "t", "<p>h</p>")


def test_email_user_without_at_sign(monkeypatch):
    _fake_smtp(monkeypatch, {})
    monkeypatch.setenv("SMTP_USER", "just.a.name")
    monkeypatch.setenv("SMTP_PASS", "abcdefghijklmnop")
    with pytest.raises(SystemExit, match="FULL Gmail address"):
        watcher.send_email("s", "t", "<p>h</p>")


def test_email_both_ports_fail(monkeypatch):
    _fake_smtp(monkeypatch, {465: "disconnect", 587: "disconnect"})
    monkeypatch.setenv("SMTP_USER", "me@gmail.com")
    monkeypatch.setenv("SMTP_PASS", "abcdefghijklmnop")
    with pytest.raises(SystemExit, match="Critical security alert"):
        watcher.send_email("s", "t", "<p>h</p>")
