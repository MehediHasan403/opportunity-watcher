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
