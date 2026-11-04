"""
SALTO-YOUTH European Training Calendar scanner.

1. Walks the calendar's browse pages and collects every upcoming training.
2. For each training not seen before, opens its detail page and reads the
   fields: type, dates, place, application deadline, who can take part,
   working language, organiser and the three cost sections.
3. Checks it against your requirements in config.yaml (open to people living
   in Germany, at least one topic keyword, deadline not passed) and tags the
   costs (fee / accommodation / travel) so you can see at a glance what's paid.

The parser reads the page *text*, not CSS classes, so small design changes on
the SALTO site don't break it.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Callable

from bs4 import BeautifulSoup

BASE = "https://www.salto-youth.net"
BROWSE = BASE + "/tools/european-training-calendar/browse/"
DETAIL_RE = re.compile(r"/tools/european-training-calendar/training/([a-z0-9\-]+)\.(\d+)/?", re.I)

KNOWN_TYPES = [
    "Training Course", "Seminar", "Study Visit", "Partnership-building Activity",
    "E-learning", "Conference", "Youth Exchange", "Other",
]

# words that end a section when reading label -> value blocks
SECTION_LABELS = [
    "and recommended for", "Accessibility info", "Working language", "Organiser",
    "Co-organiser", "Contact for questions", "Costs", "Participation fee",
    "Accommodation and food", "Travel reimbursement", "Participants are entitled",
    "Youthpass", "Back to previous list", "Disclaimer", "Available downloads",
]


# ------------------------------------------------------------------ text utils

def _norm(s: str) -> str:
    s = s.replace("–", "-").replace("—", "-").replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def _page_text_lines(html: str) -> tuple[BeautifulSoup, list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "form"]):
        tag.decompose()
    for tag in soup.find_all(True):
        if tag.attrs is None:   # already removed together with a parent
            continue
        marker = " ".join([tag.get("id") or ""] + list(tag.get("class") or [])).lower()
        if any(k in marker for k in ("menu", "navigation", "breadcrumb", "cookie", "footer")):
            tag.decompose()
    # icons carry country lists in title="..." - make that text visible
    for el in soup.find_all(attrs={"title": True}):
        if el.attrs is not None and not el.get_text(strip=True):
            el.append(f" ({el['title']}) ")
    lines = [_norm(ln) for ln in soup.get_text("\n").splitlines()]
    return soup, [ln for ln in lines if ln]


def _index(lines: list[str], pred: Callable[[str], bool], start: int = 0) -> int:
    for i in range(start, len(lines)):
        if pred(lines[i]):
            return i
    return -1


def _starts(line: str, label: str) -> bool:
    return line.lower().startswith(label.lower())


def _block_after(lines: list[str], label: str, start: int = 0, max_lines: int = 12) -> str:
    """Text that follows `label` (same line after the colon, plus following
    lines) until the next known section label."""
    i = _index(lines, lambda ln: _starts(ln, label), start)
    if i < 0:
        return ""
    first = lines[i][len(label):].lstrip(" :(s)").strip()
    parts = [first] if first else []
    for ln in lines[i + 1:i + 1 + max_lines]:
        if any(_starts(ln, lab) for lab in SECTION_LABELS if lab.lower() != label.lower()):
            break
        parts.append(ln)
    return _norm(" ".join(parts))


def parse_date(s: str) -> datetime | None:
    s = _norm(s)
    for fmt in ("%d %B %Y", "%d %b %Y", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# ------------------------------------------------------------------ listing

def parse_listing(html: str) -> dict[str, str]:
    """Return {training_id: absolute_url} for every training linked on a page."""
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        m = DETAIL_RE.search(a["href"])
        if m:
            tid = m.group(2)
            out.setdefault(tid, f"{BASE}/tools/european-training-calendar/training/{m.group(1)}.{tid}/")
    return out


def list_trainings(fetch, max_pages: int = 20, page_size: int = 10, pause: float = 1.0) -> dict[str, str]:
    found: dict[str, str] = {}
    for page in range(max_pages):
        url = f"{BROWSE}?b_offset={page * page_size}&b_limit={page_size}&b_order=applicationDeadline"
        ids = parse_listing(fetch(url).text)
        new = {k: v for k, v in ids.items() if k not in found}
        if not new:
            break
        found.update(new)
        time.sleep(pause)
    if not found:
        raise RuntimeError("SALTO browse page returned no trainings - the site layout may have changed")
    return found


# ------------------------------------------------------------------ detail page

def parse_detail(html: str, url: str) -> dict:
    soup, lines = _page_text_lines(html)

    # title: the h1 that isn't the site name
    title = ""
    for h in soup.find_all(["h1", "h2"]):
        t = _norm(h.get_text(" "))
        if t and t.lower() not in ("european training calendar", "salto-youth", "salto youth") and len(t) > 6:
            title = t
            break
    if not title:
        m = DETAIL_RE.search(url)
        title = m.group(1).replace("-", " ").title() if m else url

    t_idx = _index(lines, lambda ln: ln == title)
    t_idx = max(t_idx, 0)

    # "1-5 December 2026 | Antalya, Türkiye"
    d_idx = _index(lines, lambda ln: "|" in ln and re.search(r"\b20\d\d\b", ln) is not None and len(ln) < 160, t_idx)
    dates, place = "", ""
    if d_idx >= 0:
        dates, _, place = (p.strip() for p in lines[d_idx].partition("|"))

    # activity type: a short line near the title that starts with a known type
    a_type = ""
    for ln in lines[t_idx:t_idx + 6] + lines[max(d_idx - 3, 0):d_idx + 1 if d_idx >= 0 else 0]:
        if len(ln) < 60 and any(ln.lower().startswith(k.lower()) for k in KNOWN_TYPES):
            a_type = ln
            break

    full = "\n".join(lines)
    m = re.search(r"Application deadline[^:]{0,40}:\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4})", full)
    deadline = m.group(1) if m else ""
    m = re.search(r"Date of selection[^:]{0,40}:\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4})", full)
    selection = m.group(1) if m else ""
    m = re.search(r"for\s+(\d+)\s+participants", full)
    participants = m.group(1) if m else ""

    # who can apply: text between "for N participants" and "and recommended for"
    from_text = ""
    p_idx = _index(lines, lambda ln: re.search(r"\bparticipants\b", ln) is not None and ln.lower().startswith("for"))
    r_idx = _index(lines, lambda ln: _starts(ln, "and recommended for"))
    if p_idx >= 0 and r_idx > p_idx:
        chunk = " ".join(lines[p_idx + 1:r_idx])
        from_text = _norm(re.sub(r"^from\s+", "", chunk, flags=re.I))
    if not from_text:  # listing-style wording
        from_text = _block_after(lines, "This activity is for participants from")

    recommended = _block_after(lines, "and recommended for")
    language = _block_after(lines, "Working language", max_lines=2)
    organiser = _block_after(lines, "Organiser", max_lines=2)

    c_idx = _index(lines, lambda ln: ln.lower() == "costs")
    c_idx = max(c_idx, 0)
    fee = _block_after(lines, "Participation fee", c_idx)
    accommodation = _block_after(lines, "Accommodation and food", c_idx)
    travel = _block_after(lines, "Travel reimbursement", c_idx, max_lines=20)

    # description: text after the dates line until the first sidebar/footer block
    desc = ""
    if d_idx >= 0:
        stop = _index(lines, lambda ln: any(_starts(ln, s) for s in (
            "Your kind of training", "Training overview", "Available downloads",
            "Back to previous list", "Disclaimer", "Apply now")), d_idx + 1)
        stop = stop if stop > d_idx else min(len(lines), d_idx + 60)
        desc = _norm(" ".join(lines[d_idx + 1:stop]))
    if len(desc) < 80:
        meta = soup.find("meta", attrs={"name": "description"})
        if meta and meta.get("content"):
            desc = _norm(meta["content"]) + " " + desc
    disc = _index(lines, lambda ln: _starts(ln, "Disclaimer"))
    core = _norm(" ".join(lines[t_idx:disc if disc > t_idx else len(lines)]))

    return {
        "url": url, "title": title, "type": a_type, "dates": dates, "place": place,
        "deadline": deadline, "selection": selection, "participants": participants,
        "from": from_text, "recommended": recommended, "language": language,
        "organiser": organiser, "fee": fee, "accommodation": accommodation,
        "travel": travel, "description": desc, "core_text": core,
    }


# ------------------------------------------------------------------ matching

def _has(text: str, term: str) -> bool:
    return re.search(r"(?<!\w)" + re.escape(term.lower()) + r"(?!\w)", text.lower()) is not None


def cost_flags(t: dict) -> list[str]:
    flags = []
    fee, acc, trv = t["fee"].lower(), t["accommodation"].lower(), t["travel"].lower()
    if re.search(r"^no\b|\bno participation fee\b|\bfree\b|\bnone\b|\bno fee\b|\bnot charged\b|\b0 ?(€|eur)", fee):
        flags.append("✅ No participation fee")
    elif "national agency" in fee or " na " in f" {fee} ":
        flags.append("❔ Fee: ask your National Agency")
    elif fee:
        flags.append("⚠️ Participation fee: see page")
    if re.search(r"cover|provid|organis|organiz|paid|included|grant", acc):
        flags.append("✅ Accommodation & food covered")
    if re.search(r"reimburs|cover|refund|\d+\s*(€|eur)|(€|eur)\s*\d+", trv):
        m = re.search(r"germany[^0-9€\n]{0,25}(\d{2,4})\s*(?:€|eur)|germany[^0-9€\n]{0,25}(?:€|eur)\s*(\d{2,4})", trv)
        cap = f" (Germany: up to {m.group(1) or m.group(2)} €)" if m else ""
        flags.append("✅ Travel reimbursed" + cap)
        if "green travel" in trv and m:
            flags[-1] += " - green travel required"
    if "country of residence" in (fee + " " + trv):
        flags.append("👉 Ask JUGEND für Europa (Germany's National Agency) before applying")
    return flags


def _today() -> datetime:
    return datetime.now(timezone.utc)


def evaluate(t: dict, req: dict, today: datetime | None = None) -> tuple[bool, dict]:
    """Return (matches, info). info has matched topics, eligibility, days left."""
    today = today or _today()
    info: dict = {"topics": [], "eligibility": "", "days_left": None, "reason": ""}

    # deadline
    dl = parse_date(t["deadline"]) if t["deadline"] else None
    if dl:
        info["days_left"] = (dl.date() - today.date()).days
        if req.get("skip_passed_deadlines", True) and info["days_left"] < 0:
            info["reason"] = "deadline passed"
            return False, info

    # activity type
    types = [x.lower() for x in req.get("activity_types", []) or []]
    if types and not any(t["type"].lower().startswith(x) for x in types):
        info["reason"] = f"type {t['type']!r} not wanted"
        return False, info

    # eligibility for someone living in Germany
    open_to = req.get("open_to", [])
    if t["from"]:
        hit = next((c for c in open_to if _has(t["from"], c)), None)
        if hit:
            info["eligibility"] = f"✅ Open to you ({hit})"
        else:
            info["reason"] = "not open to participants from Germany"
            return False, info
    elif req.get("include_unknown_eligibility", True):
        info["eligibility"] = "❔ Check who can apply"
    else:
        info["reason"] = "eligibility unknown"
        return False, info

    # topics
    desc_ok = len(t["description"]) >= 80
    text = " ".join([t["title"], t["description"], t["recommended"]]) if desc_ok else t["core_text"]
    for word in req.get("exclude_words", []) or []:
        if _has(text, word):
            info["reason"] = f"excluded word {word!r}"
            return False, info
    info["topics"] = [k for k in req.get("topics", []) if _has(text, k)]
    if not info["topics"]:
        info["reason"] = "no topic keyword"
        return False, info
    return True, info


# ------------------------------------------------------------------ entry point

def scan(fetch, req: dict, seen: list[str], log=print, pause: float = 1.0) -> tuple[list[dict], list[str]]:
    """Return (matching new trainings, updated seen-id list)."""
    listing = list_trainings(fetch, max_pages=req.get("max_pages", 20), pause=pause)
    seen_set = set(seen)
    new_ids = [tid for tid in listing if tid not in seen_set]
    log(f"       SALTO: {len(listing)} upcoming trainings listed, {len(new_ids)} not checked before")
    matches = []
    for tid in new_ids:
        url = listing[tid]
        try:
            t = parse_detail(fetch(url).text, url)
        except Exception as e:   # one broken page shouldn't stop the scan; retry next run
            log(f"       SALTO: could not read {url}: {e}")
            continue
        ok, info = evaluate(t, req)
        seen.append(tid)
        log(f"       {'MATCH' if ok else 'skip '} {t['title'][:70]!r}"
            + (f" -> {', '.join(info['topics'])}" if ok else f" ({info['reason']})"))
        if ok:
            t.update(info)
            t["id"] = tid
            t["costs"] = cost_flags(t)
            matches.append(t)
        time.sleep(pause)
    matches.sort(key=lambda x: x["days_left"] if x["days_left"] is not None else 9999)
    return matches, seen
