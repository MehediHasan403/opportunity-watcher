#!/usr/bin/env python3
"""
Opportunity Watcher
-------------------
Checks the SALTO-YOUTH European Training Calendar, a set of web searches,
RSS feeds and web pages (all set in config.yaml). When something new
matches your requirements, it emails you.

  python watcher.py               normal run: check, email if new, save state
  python watcher.py --dry-run     check and print, no email, nothing saved
  python watcher.py --test-email  send one test email

Email settings come from environment variables (GitHub Secrets):
  SMTP_USER  your Gmail address
  SMTP_PASS  a Gmail App Password (not your normal password)
  EMAIL_TO   where alerts go (defaults to SMTP_USER)
  SMTP_HOST / SMTP_PORT  optional, default smtp.gmail.com / 465
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse, urlunparse

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

import salto

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "config.yaml"
STATE_FILE = HERE / "state.json"

USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36 opportunity-watcher/2.0")
TIMEOUT = 30
MAX_SEEN_PER_SOURCE = 4000
MAX_ITEMS_PER_SOURCE_IN_EMAIL = 15
MAX_CHANGE_LINES = 12
FAIL_ALERT_AFTER = 4          # consecutive failed runs before you're told
SALTO_NAME = "SALTO European Training Calendar"


# ================================================================ helpers

def now() -> datetime:
    return datetime.now(timezone.utc)


def clean_text(s: str) -> str:
    if not s:
        return ""
    s = BeautifulSoup(s, "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def normalize_url(url: str) -> str:
    """Unwrap Google redirect links and drop tracking parameters."""
    if not url:
        return ""
    p = urlparse(url)
    if "google." in p.netloc and p.path == "/url":
        real = (parse_qs(p.query).get("url") or parse_qs(p.query).get("q") or [""])[0]
        if real:
            p = urlparse(real)
    query = "&".join(x for x in p.query.split("&")
                     if x and not x.lower().startswith(("utm_", "fbclid", "gclid", "ved=", "usg=", "oc=")))
    return urlunparse((p.scheme, p.netloc.lower(), p.path.rstrip("/") or "/", "", query, ""))


def item_id(url: str, title: str) -> str:
    return hashlib.sha1((normalize_url(url) or title.lower()).encode()).hexdigest()[:16]


def term_found(text: str, term: str) -> bool:
    """Whole-word, case-insensitive: 'BIP' won't match 'bipolar', 'C++' works."""
    return re.search(r"(?<!\w)" + re.escape(term.lower()) + r"(?!\w)", text.lower()) is not None


def match_keywords(text: str, include: list[str], exclude: list[str]) -> list[str]:
    if any(term_found(text, t) for t in exclude):
        return []
    return [t for t in include if term_found(text, t)]


_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en;q=0.9,de;q=0.6"})


def fetch(url: str) -> requests.Response:
    last = None
    for attempt in range(3):
        try:
            r = _session.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = e
            time.sleep(3 * (attempt + 1))
    raise last  # type: ignore[misc]


# ================================================================ generic sources

def check_feed(src, include, exclude) -> list[dict]:
    feed = feedparser.parse(fetch(src["url"]).content)
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"not a valid feed ({feed.bozo_exception})")
    items = []
    for e in feed.entries:
        title = clean_text(e.get("title", ""))
        summary = clean_text(e.get("summary", ""))
        link = normalize_url(e.get("link", ""))
        if src.get("filter", True):
            hits = match_keywords(f"{title} {summary}", include, exclude)
            if not hits:
                continue
        else:
            hits = ["search result"]
        items.append({"id": item_id(link, title), "title": title or link, "url": link,
                      "snippet": summary[:280] if summary != title else "", "matched": hits,
                      "date": e.get("published", "")[:16]})
    return items


def check_links(src, include, exclude) -> list[dict]:
    resp = fetch(src["url"])
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    items, here = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = normalize_url(urljoin(resp.url, href))
        text = clean_text(a.get_text(" "))
        parent = a.find_parent(["li", "article", "div", "p", "tr", "section"])
        context = clean_text(parent.get_text(" "))[:400] if parent else ""
        hits = match_keywords(f"{text} {url} {context}", include, exclude)
        iid = item_id(url, text)
        if not hits or iid in here:
            continue
        here.add(iid)
        items.append({"id": iid, "title": text or url, "url": url,
                      "snippet": context[:280] if context != text else "", "matched": hits})
    return items


def page_lines(html_text: str) -> list[str]:
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer"]):
        tag.decompose()
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in soup.get_text("\n").splitlines()]
    return [ln for ln in lines if len(ln) > 3]


def check_changes(src, st) -> list[dict]:
    lines = page_lines(fetch(src["url"]).text)
    digest = hashlib.sha256("\n".join(lines).encode()).hexdigest()
    old_hash, old_lines = st.get("hash"), set(st.get("lines", []))
    st["hash"], st["lines"] = digest, lines[:1500]
    if old_hash is None or old_hash == digest:
        return []
    added = [ln for ln in lines if ln not in old_lines][:MAX_CHANGE_LINES]
    if not added:
        return []
    return [{"id": digest[:16], "title": f"Page updated: {src['name']}", "url": src["url"],
             "snippet": " | ".join(added), "matched": ["page changed"]}]


def search_sources(cfg) -> list[dict]:
    """Turn every search term into a Google News search feed (no account needed)."""
    s = cfg.get("web_search", {})
    if not s.get("enabled", True):
        return []
    window = s.get("recent_days", 30)
    out = []
    for term in s.get("terms", []):
        q = quote_plus(f"{term} when:{window}d")
        out.append({"name": f"Web search: {term}", "mode": "feed", "filter": s.get("filter", True),
                    "url": f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en",
                    "_search": True})
    return out


# ================================================================ email

def _esc(s) -> str:
    return html.escape(str(s or ""))


def salto_card_html(t: dict) -> str:
    dl = t["deadline"] or "see page"
    if t.get("days_left") is not None:
        d = t["days_left"]
        dl += f" &nbsp;<b style='color:{'#c0392b' if d <= 7 else '#1e8449'}'>({'today' if d == 0 else f'{d} days left'})</b>"
    rows = [
        ("When / where", f"{_esc(t['dates'])} | {_esc(t['place'])}"),
        ("Apply by", dl + (f" &nbsp;· selection {_esc(t['selection'])}" if t["selection"] else "")),
        ("Who can apply", f"{_esc(t['eligibility'])}<br><span style='color:#666'>{_esc(t['from'][:300])}</span>"),
        ("Recommended for", _esc(t["recommended"][:200])),
        ("Organiser", _esc(t["organiser"][:120])),
        ("Language", _esc(t["language"])),
        ("Costs", "<br>".join(_esc(c) for c in t["costs"]) or "see page"),
        ("Matched", _esc(", ".join(t["topics"]))),
    ]
    rows_html = "".join(
        f"<tr><td style='color:#666;padding:2px 10px 2px 0;vertical-align:top;white-space:nowrap'>{k}</td>"
        f"<td style='padding:2px 0'>{v}</td></tr>" for k, v in rows if v and v != " | ")
    return (
        "<div style='border:1px solid #ddd;border-radius:8px;padding:12px 14px;margin:0 0 14px'>"
        f"<div style='color:#888;font-size:12px'>{_esc(t['type'])}</div>"
        f"<a href='{_esc(t['url'])}' style='font-size:16px;font-weight:700;color:#1a4d8f'>{_esc(t['title'])}</a>"
        f"<p style='margin:6px 0 8px;font-size:13px'>{_esc(t['description'][:350])}{'…' if len(t['description']) > 350 else ''}</p>"
        f"<table style='font-size:13px;border-collapse:collapse'>{rows_html}</table></div>")


def salto_card_text(t: dict) -> str:
    left = f" ({t['days_left']} days left)" if t.get("days_left") is not None else ""
    return "\n".join([
        f"* {t['title']}  [{t['type']}]",
        f"  {t['dates']} | {t['place']}",
        f"  Apply by: {t['deadline'] or 'see page'}{left}",
        f"  {t['eligibility']} - from: {t['from'][:200]}",
        f"  Costs: {'; '.join(t['costs']) or 'see page'}",
        f"  Matched: {', '.join(t['topics'])}",
        f"  {t['url']}", ""])


def build_email(salto_items, other, problems, first_run) -> tuple[str, str, str]:
    n_salto, n_other = len(salto_items), sum(len(v) for v in other.values())
    if first_run:
        subject = f"Opportunity watcher is live: {n_salto} SALTO training(s) match you now"
        intro = ("Your watcher ran for the first time. Below are the SALTO trainings that match your "
                 "requirements right now. From now on you only get an email when something new appears.")
    else:
        bits = []
        if n_salto:
            bits.append(f"{n_salto} new SALTO training{'s' if n_salto > 1 else ''}")
        if n_other:
            bits.append(f"{n_other} other new item{'s' if n_other > 1 else ''}")
        subject = ("New: " + " + ".join(bits)) if bits else "Opportunity watcher: some sources keep failing"
        intro = "New opportunities matching your requirements since the last check:"

    t_parts, h_parts = [intro, ""], [f"<p style='font-size:15px'>{_esc(intro)}</p>"]
    if salto_items:
        t_parts.append(f"== {SALTO_NAME} ==")
        h_parts.append(f"<h2 style='font-size:17px;margin:18px 0 8px'>🎓 {SALTO_NAME}</h2>")
        for t in salto_items:
            t_parts.append(salto_card_text(t))
            h_parts.append(salto_card_html(t))
    for source, items in other.items():
        t_parts.append(f"== {source} ==")
        h_parts.append(f"<h3 style='margin:18px 0 6px;color:#1a4d8f;font-size:15px'>{_esc(source)}</h3>"
                       "<ul style='padding-left:18px'>")
        for it in items:
            kw = ", ".join(it["matched"])
            t_parts.append(f"- {it['title']}\n  {it['url']}\n  [{kw}] {it['snippet']}")
            h_parts.append("<li style='margin-bottom:9px'>"
                           f"<a href='{_esc(it['url'])}' style='font-weight:600'>{_esc(it['title'][:160])}</a>"
                           f"<br><span style='color:#777;font-size:12px'>matched: {_esc(kw)}"
                           f"{' · ' + _esc(it['date']) if it.get('date') else ''}</span>"
                           + (f"<br><span style='font-size:13px'>{_esc(it['snippet'])}</span>" if it["snippet"] else "")
                           + "</li>")
        h_parts.append("</ul>")
        t_parts.append("")
    if problems:
        t_parts += ["Sources failing for a while (check the URL in config.yaml):"] + [f"- {p}" for p in problems]
        h_parts.append("<p style='color:#a33;font-size:12px'><b>Sources failing for a while:</b><br>"
                       + "<br>".join(_esc(p) for p in problems) + "</p>")
    foot = f"Checked {now():%Y-%m-%d %H:%M} UTC · edit config.yaml in your GitHub repo to change what's watched."
    t_parts.append(foot)
    h_parts.append(f"<p style='color:#999;font-size:11px'>{_esc(foot)}</p>")
    return (subject, "\n".join(t_parts),
            "<div style='font-family:Arial,sans-serif;max-width:680px'>" + "".join(h_parts) + "</div>")


def send_email(subject: str, text: str, html_body: str) -> None:
    user, password = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS")
    to = os.environ.get("EMAIL_TO") or user
    host, port = os.environ.get("SMTP_HOST", "smtp.gmail.com"), int(os.environ.get("SMTP_PORT", "465"))
    if not user or not password:
        raise SystemExit("SMTP_USER / SMTP_PASS are not set. Add them as GitHub Secrets.")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, f"Opportunity Watcher <{user}>", to
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            s.login(user, password.replace(" ", ""))
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(user, password.replace(" ", ""))
            s.send_message(msg)


# ================================================================ main

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"sources": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def run(dry_run: bool) -> int:
    cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
    inc = cfg.get("keywords", {}).get("include", [])
    exc = cfg.get("keywords", {}).get("exclude", []) or []
    state = load_state()
    first_run = not state["sources"]
    salto_items: list[dict] = []
    other: dict[str, list[dict]] = {}
    problems: list[str] = []

    def note_result(name, url, error=None):
        st = state["sources"].setdefault(name, {})
        if error is None:
            st["fails"] = 0
            return
        st["fails"] = st.get("fails", 0) + 1
        print(f"[fail] {name}: {error}")
        if st["fails"] == FAIL_ALERT_AFTER or (st["fails"] > FAIL_ALERT_AFTER and st["fails"] % 42 == 0):
            problems.append(f"{name} ({url}): {type(error).__name__}: {str(error)[:120]}")

    # ---- 1. SALTO
    s_cfg = cfg.get("salto", {})
    if s_cfg.get("enabled", True):
        st = state["sources"].setdefault(SALTO_NAME, {})
        try:
            found, seen = salto.scan(fetch, s_cfg, list(st.get("seen", [])), pause=s_cfg.get("pause_seconds", 1.0))
            st["seen"] = seen[-MAX_SEEN_PER_SOURCE:]
            salto_items = found
            note_result(SALTO_NAME, salto.BROWSE)
            print(f"[ok]   {SALTO_NAME}: {len(found)} new matching training(s)")
        except Exception as e:
            note_result(SALTO_NAME, salto.BROWSE, e)

    # ---- 2. web searches, feeds, pages
    for src in search_sources(cfg) + cfg.get("sources", []):
        if not src.get("enabled", True) or "PASTE_" in src.get("url", ""):
            continue
        name, mode = src["name"], src.get("mode", "links")
        st = state["sources"].setdefault(name, {})
        brand_new = "seen" not in st and "hash" not in st
        try:
            if mode == "changes":
                new_items = check_changes(src, st)
                found_n = len(new_items)
            else:
                s_inc = src.get("keywords", inc)
                s_exc = exc + (src.get("exclude", []) or [])
                found = check_feed(src, s_inc, s_exc) if mode == "feed" else check_links(src, s_inc, s_exc)
                found_n = len(found)
                seen = st.get("seen", [])
                seen_set = set(seen)
                new_items = [it for it in found if it["id"] not in seen_set]
                st["seen"] = (seen + [it["id"] for it in new_items])[-MAX_SEEN_PER_SOURCE:]
            note_result(name, src["url"])
            # The first time a source is checked, its existing items are only remembered
            # (old news isn't news). Set report_on_first_run: true to get them anyway.
            if brand_new and not src.get("report_on_first_run", False):
                print(f"[ok]   {name}: {found_n} matching, remembered as baseline")
                continue
            if new_items:
                other[name] = new_items[:MAX_ITEMS_PER_SOURCE_IN_EMAIL]
            print(f"[ok]   {name}: {found_n} matching, {len(new_items)} new")
        except Exception as e:
            note_result(name, src["url"], e)
        time.sleep(0.5)

    total = len(salto_items) + sum(len(v) for v in other.values())
    print(f"\n{total} new item(s) to report.")

    if dry_run:
        for t in salto_items:
            print("\n" + salto_card_text(t))
        for n, items in other.items():
            print(f"\n== {n} ==")
            for it in items:
                print(f"- {it['title'][:110]}\n  {it['url']}")
        return 0

    if total or problems:
        subject, text, html_body = build_email(salto_items, other, problems, first_run)
        send_email(subject, text, html_body)
        print(f"Email sent: {subject}")

    # heartbeat once a week keeps the repo 'active' so GitHub never pauses the schedule
    hb = state.get("heartbeat")
    if not hb or now() - datetime.fromisoformat(hb) > timedelta(days=7):
        state["heartbeat"] = now().isoformat(timespec="seconds")
    save_state(state)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print results, send and save nothing")
    ap.add_argument("--test-email", action="store_true", help="send one test email and exit")
    args = ap.parse_args()
    if args.test_email:
        send_email("Opportunity watcher: test email ✅",
                   "If you can read this, your email settings work.",
                   "<p>If you can read this, your email settings work. ✅</p>")
        print("Test email sent.")
        return 0
    return run(args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
