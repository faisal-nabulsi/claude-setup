"""Berkeley class schedule (classes.berkeley.edu) and grade distributions (Berkeleytime). No login.

classes.berkeley.edu is a Drupal site. Its section pages ship the live seat counts inside the
page's drupal-settings JSON (ucb.enrollment.available), and search result pages list open seats per
section, so plain HTTPS GETs are enough; no browser. The site rate-limits (plain curl got 403 and
parallel requests got 429 on 2026-11-03), so every request here:
  - goes out one at a time, at least MIN_GAP seconds after the previous one, across processes
    (a timestamp file, because mcp_bridge.py starts a fresh server per call);
  - is cached on disk for CACHE_TTL seconds;
  - backs off once on 429/403 and then reports the error instead of hammering.
"""
from __future__ import annotations

import fcntl
import hashlib
import html as htmllib
import json
import re
import time
from pathlib import Path

import httpx

SITE = "https://classes.berkeley.edu"
STATE = Path.home() / ".local" / "share" / "calcentral-mcp"
CACHE = STATE / "cache"
THROTTLE = STATE / "classes-throttle"
TERMS = STATE / "terms.json"
MIN_GAP = 3.0
CACHE_TTL = 600
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/157.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-US,en;q=0.9"}


# ---------- polite fetching ----------

def _throttled_get(url: str, params=None) -> str:
    STATE.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(exist_ok=True)
    key = hashlib.sha256((url + json.dumps(params or [], sort_keys=True)).encode()).hexdigest()[:32]
    cached = CACHE / f"{key}.html"
    if cached.exists() and time.time() - cached.stat().st_mtime < CACHE_TTL:
        return cached.read_text()
    with open(THROTTLE, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)  # one request at a time, across processes
        try:
            fh.seek(0)
            last = float(fh.read().strip() or 0)
            wait = MIN_GAP - (time.time() - last)
            if wait > 0:
                time.sleep(wait)
            text = None
            for attempt in range(2):
                r = httpx.get(url, params=params, headers=HEADERS, timeout=30, follow_redirects=True)
                fh.seek(0)
                fh.truncate()
                fh.write(str(time.time()))
                fh.flush()
                if r.status_code in (429, 403) and attempt == 0:
                    time.sleep(min(float(r.headers.get("retry-after", 30) or 30), 60))
                    continue
                if r.status_code == 404:
                    raise LookupError(f"not found: {r.url}")
                r.raise_for_status()
                text = r.text
                break
            if text is None:
                raise RuntimeError(f"classes.berkeley.edu kept refusing ({r.status_code}); try again in a few minutes")
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    for old in CACHE.glob("*.html"):  # keep the cache small
        if time.time() - old.stat().st_mtime > 3600:
            old.unlink(missing_ok=True)
    cached.write_text(text)
    return text


def _text(fragment: str) -> str:
    return " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", fragment or "")).split())


def _grab(pattern: str, s: str, flags=re.S) -> str:
    m = re.search(pattern, s, flags)
    return _text(m.group(1)) if m else ""


# ---------- terms ----------

def norm_term(term: str | None) -> str:
    """'spring 2027' / '2027 Spring' / 'SP27' -> 'Spring 2027'. Default: the next term people enroll for."""
    if not term:
        lt = time.localtime()
        # Nov-Apr -> Spring of next/current year is the active enrollment term; May-Oct -> Fall.
        if lt.tm_mon >= 10:
            return f"Spring {lt.tm_year + 1}"
        if lt.tm_mon <= 3:
            return f"Spring {lt.tm_year}"
        return f"Fall {lt.tm_year}"
    t = term.strip().lower().replace("sessions", "").replace("  ", " ")
    m = re.search(r"(20\d\d)", t) or re.search(r"(\d\d)$", t)
    year = m.group(1) if m else ""
    if len(year) == 2:
        year = "20" + year
    season = "Spring" if t.startswith("sp") or "spring" in t else "Fall" if t.startswith("fa") or "fall" in t \
        else "Summer" if t.startswith("su") or "summer" in t else ""
    if not (season and year):
        raise ValueError(f"can't read term {term!r}; use e.g. 'Spring 2027'")
    return f"{season} {year}"


def _learn_terms(page: str) -> dict:
    found = {}
    for m in re.finditer(r'term%3A(\d+)"[^>]*>(.*?)</a>', page, re.S):
        label = re.sub(r"\s*\(\d+\)\s*$", "", _text(m.group(2))).replace("Summer Sessions", "Summer")
        if re.fullmatch(r"(Spring|Summer|Fall) 20\d\d", label):
            found[label] = m.group(1)
    return found


def term_id(term_name: str) -> str | None:
    known = json.loads(TERMS.read_text()) if TERMS.exists() else {}
    if term_name in known:
        return known[term_name]
    page = _throttled_get(SITE + "/search/class", params=[("search", "")])
    known.update(_learn_terms(page))
    STATE.mkdir(parents=True, exist_ok=True)
    TERMS.write_text(json.dumps(known, indent=1))
    return known.get(term_name)


# ---------- parsing ----------

def _settings(page: str) -> dict:
    m = re.search(r'<script type="application/json" data-drupal-selector="drupal-settings-json">(.*?)</script>', page, re.S)
    return json.loads(m.group(1)) if m else {}


def _meetings(block: str, prefix: str) -> list[dict]:
    out = []
    for m in re.finditer(rf'<div class="{prefix}--meeting-details">(.*?)</div>\s*</div>', block, re.S):
        seg = m.group(1) + "</div>"
        out.append({
            "dates": _grab(rf'{prefix}--meeting-dates">(.*?)</div>', seg),
            "days": _grab(rf'{prefix}--meeting-days">(.*?)</div>', seg),
            "time": _grab(rf'{prefix}--meeting-time">(.*?)</div>', seg),
            "location": _grab(rf'{prefix}--location">(.*?)</div>', seg),
        })
    return [x for x in out if any(x.values())]


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_section(page: str, url: str) -> dict:
    s = _settings(page).get("ucb", {})
    avail = (s.get("enrollment") or {}).get("available") or {}
    es = avail.get("enrollmentStatus") or {}
    enrolled, cap = _int(es.get("enrolledCount")), _int(es.get("maxEnroll"))
    wl, wl_max = _int(es.get("waitlistedCount")), _int(es.get("maxWaitlist"))
    reserved_html = ""
    m = re.search(r'id="section-reserved-seats".*?<div class="section-content">(.*?)</section>', page, re.S)
    if m:
        reserved_html = m.group(1)
    reserved_lines = [_text(x) for x in re.findall(r"<(?:li|tr|p)[^>]*>(.*?)</(?:li|tr|p)>", reserved_html, re.S)]
    reserved_lines = [x for x in reserved_lines if x]
    phases = [{"phase": p.get("phase_name"), "dates": p.get("phase_dates")}
              for p in ((s.get("termDetails") or {}).get("enrollmentPhases") or [])]
    return {
        "url": url,
        "term": _grab(r'sf--title--term">(.*?)</div>', page),
        "section": _grab(r'<div id="block-ucb-page-title">.*?<span>(.*?)</span>', page),
        "title": _grab(r'<h2 class="sf--course-title">(.*?)</h2>', page),
        "class_number": _grab(r'<span>Class #:</span>\s*(\d+)', page),
        "units": _grab(r'<span>Units:</span>\s*([^<]+)<', page),
        "instructors": _grab(r'sf--instructors">\s*<p>(.*?)</p>', page),
        "meetings": _meetings(page, "sf"),
        "instruction_mode": _grab(r'<strong>Instruction Mode:</strong>\s*<span>(.*?)</span>', page),
        "status": ((es.get("status") or {}).get("description")),
        "enrolled": enrolled,
        "capacity": cap,
        "open_seats": (cap - enrolled) if cap is not None and enrolled is not None else None,
        "waitlisted": wl,
        "waitlist_max": wl_max,
        "reserved_seats_total": _int(es.get("reservedCount")),
        "reserved_seats_open": _int(es.get("openReserved")),
        "reserved_seats_detail": _text(reserved_html) if not reserved_lines else reserved_lines,
        "enrollment_phases": phases,
        "seat_data_note": "Live from classes.berkeley.edu (CalCentral's own number can lag a few minutes).",
    }


def parse_search(page: str) -> dict:
    total = _grab(r'<div class="view-header">(.*?)</div>', page)
    items = []
    for art in re.findall(r'<article data-history-node-id="\d+" class="st">(.*?)</article>', page, re.S):
        href = re.search(r'<a href="(/content/[^"]+)"', art)
        items.append({
            "section": _grab(r'st--section-name-wraper">\s*<a[^>]*>(.*?)</a>', art),
            "title": _grab(r'<div class="st--title">\s*<h2>(.*?)</h2>', art),
            "class_number": _grab(r'<span>Class #:</span>\s*(\d+)', art),
            "units": _grab(r'<span>Units:</span>\s*([^<]+)<', art),
            "instructors": _grab(r'st--instructors">(.*?)</div>', art),
            "meetings": _meetings(art, "st"),
            "seats": " / ".join(_text(x) for x in re.findall(r'<p aria-describedby="open-seats-label-\d+">(.*?)</p>', art, re.S)),
            "url": SITE + href.group(1) if href else "",
        })
    nxt = bool(re.search(r'<a [^>]*rel="next"', page) or 'pager__item--next' in page)
    return {"total": total, "results": items, "more_pages": nxt}


# ---------- public functions (used by server.py tools) ----------

def section_url(course: str, term: str, section: str = "001", kind: str = "LEC") -> str:
    m = re.fullmatch(r"\s*([A-Za-z ,&]+?)\s+([A-Za-z]?\d+[A-Za-z]*)\s*", course)
    if not m:
        raise ValueError(f"course should look like 'MATH 1A' or 'CHEM 1A', got {course!r}")
    subj = re.sub(r"[^a-z]", "", m.group(1).lower())
    num = m.group(2).lower()
    season, year = term.split()
    sec = section.zfill(3)
    return f"{SITE}/content/{year}-{season.lower()}-{subj}-{num}-{sec}-{kind.lower()}-{sec}"


def seats(course: str, term: str | None = None, section: str = "001", kind: str = "LEC") -> dict:
    term = norm_term(term)
    url = section_url(course, term, section, kind)
    try:
        page = _throttled_get(url)
    except LookupError:
        # Unusual numbering (e.g. section 002, or a SEM): find the real links through search.
        found = search(course, term=term, limit=25)
        subj_num = " ".join(course.upper().split())
        cands = [r for r in found["results"] if r["section"].upper().startswith(subj_num + " ")]
        return {"error": f"no page at {url}", "sections_found_by_search": cands}
    return parse_section(page, url)


def search(query: str = "", term: str | None = None, filters: list[str] | None = None, page: int = 0,
           limit: int = 25) -> dict:
    term = norm_term(term)
    tid = term_id(term)
    params = [("search", query)]
    if tid:
        params.append(("f[]", f"term:{tid}"))
    for f in filters or []:
        params.append(("f[]", f))
    if page:
        params.append(("page", str(page)))
    out = parse_search(_throttled_get(SITE + "/search/class", params=params))
    out["results"] = out["results"][:limit]
    out["term"] = term
    if not tid:
        out["warning"] = f"term id for {term} not found; results may span all terms"
    return out


# ---------- Berkeleytime grade distributions ----------

_Q_COURSE = """query GetCourseGradeDist($subject: String!, $number: CourseNumber!) {
    course(subject: $subject, number: $number) {
      courseId
      subject
      number
      gradeDistribution {
        average
        distribution {
          letter
          count
        }
      }
    }
  }"""

_Q_INSTR = """
  query GetGradeDistribution(
    $year: Int
    $semester: Semester
    $sessionId: SessionIdentifier
    $subject: String!
    $courseId: String!
    $classNumber: ClassNumber
    $familyName: String
    $givenName: String
  ) {
    grade(
      year: $year
      semester: $semester
      sessionId: $sessionId
      subject: $subject
      courseId: $courseId
      classNumber: $classNumber
      familyName: $familyName
      givenName: $givenName
    ) {
      average
      pnpPercentage
      distribution {
        letter
        percentage
        count
      }
    }
  }
"""

_PUNCT = set("!$&():=@[]{}|")


def _strip_ignored(q: str) -> str:
    """graphql-js stripIgnoredCharacters, enough for these queries (Berkeleytime only runs registered ops)."""
    toks = re.findall(r"\.\.\.|[!$&():=@\[\]{}|]|[_A-Za-z][_0-9A-Za-z]*|-?\d+(?:\.\d+)?", q)
    out, prev = "", False
    for t in toks:
        nonpunct = t not in _PUNCT and t != "..."
        if prev and (nonpunct or t == "..."):
            out += " "
        out += t
        prev = nonpunct
    return out


def _bt(query: str, variables: dict) -> dict:
    qid = hashlib.sha256(_strip_ignored(query).encode()).hexdigest()
    r = httpx.post("https://berkeleytime.com/api/graphql", json={"id": qid, "variables": variables},
                   headers={"User-Agent": UA, "Content-Type": "application/json"}, timeout=30)
    r.raise_for_status()
    return r.json()


def _summ(dist_list, average):
    dist = {x["letter"]: x["count"] for x in dist_list}
    letter = sum(v for k, v in dist.items() if k not in ("P", "NP"))
    a = sum(dist.get(k, 0) for k in ("A+", "A", "A-"))
    aflat = sum(dist.get(k, 0) for k in ("A+", "A"))
    return {"avg_gpa": round(average or 0, 2),
            "pct_A_range": round(100 * a / letter, 1) if letter else None,
            "pct_A_or_Aplus": round(100 * aflat / letter, 1) if letter else None,
            "letter_graded_n": letter, "distribution": dist}


def grades(course: str, instructor: str | None = None) -> dict:
    subj, num = " ".join(course.upper().split()).rsplit(" ", 1)
    d = _bt(_Q_COURSE, {"subject": subj, "number": num})
    c = (d.get("data") or {}).get("course")
    if not c or not c.get("gradeDistribution"):
        return {"course": f"{subj} {num}", "error": "Berkeleytime has no grade data for this course",
                "raw": str(d)[:200]}
    out = {"course": f"{subj} {num}", "source": "Berkeleytime (all past terms)",
           **_summ(c["gradeDistribution"]["distribution"], c["gradeDistribution"]["average"])}
    if instructor:
        given, family = (instructor.split(" ", 1) + [""])[:2] if " " in instructor else ("", instructor)
        g = (_bt(_Q_INSTR, {"subject": subj, "courseId": c["courseId"], "familyName": family.strip(),
                            "givenName": given.strip()}).get("data") or {}).get("grade")
        out["instructor"] = {"name": instructor, **(_summ(g["distribution"], g["average"]) if g else
                                                    {"error": "no data for this instructor"})}
    return out
