"""CalCentral read functions behind the MCP tools. GET only (plus fixed GraphQL *queries*), allow-listed paths only.

Feed map (read from CalCentral's front-end bundle main.*.js on 2026-11-16; the repo is private):
  /api/my/academics               semesters[] -> classes[] -> sections[] (schedule, waitlist, units), transcript[] (grades)
  /api/my/enrollment_resources    termInstructions[] (term_id, enrollmentPeriods[name, beginTime, endTime]), hasHolds, messages
  /api/my/holds                   holds[]
  /api/my/finances/billing_items  billing items (charges, payments, due dates)
  /api/campus_solutions/billing_activity, fpp_enrollment   billing summary, tuition payment plan
  /api/academics/degree_progress/ugrd   degree progress
  POST /graphql  query userAcademicProfile   gpa.cumulativeGPA, units, majors (a read-only GraphQL query)
"""
from __future__ import annotations

import json
import re
import time

from session import COOKIES, CalCentral, SignInRequired

_cc = CalCentral()

# Read-only feeds the CalCentral front end loads with GET. Anything not matching is refused, even a GET.
READ_OK = [
    r"/api/my/status", r"/api/my/am_i_logged_in", r"/api/config",
    r"/api/my/academics(/standings|/status_and_holds|/committees)?", r"/api/my/academics/terms/\d+/courses/.+",
    r"/api/my/holds", r"/api/my/registrations", r"/api/my/standings", r"/api/my/classes", r"/api/my/badges",
    r"/api/my/academic_records", r"/api/my/residency", r"/api/my/enrollment_resources", r"/api/my/bcal_schedule",
    r"/api/my/finances/billing_items(/\d+)?", r"/api/my/financial_aid_summary", r"/api/my/aid_years",
    r"/api/my/awards(_by_term)?/\d+", r"/api/my/finaid_profile/\d+", r"/api/my/eft_enrollment",
    r"/api/my/loan_history_(aid_years|cumulative|inactive|summary)", r"/api/my/housing/\d+",
    r"/api/my/tasks/(checklist_items|agreements|web_messages|b_courses_todos|b_courses_activities)",
    r"/api/my/textbooks_details", r"/api/my/sir_statuses",
    r"/api/academics/degree_progress/ugrd", r"/api/academics/transfer_credits", r"/api/academics/exam_results",
    r"/api/academics/has_exam_results", r"/api/academics/enrollment_verification",
    r"/api/academics/pnp_calculator/(calculator_values|ratio_message)",
    r"/api/campus_solutions/(billing_activity|billing_links|financial_aid_data|student_resources|fpp_enrollment)",
    r"/api/campus_solutions/financial_aid_coa",
]
_READ_RE = [re.compile(p + r"/?") for p in READ_OK]
MAX_RAW = 60_000

# The only GraphQL operation this server sends. A fixed *query* copied from CalCentral's bundle:
# never built from tool input, never a mutation.
GQL_PROFILE = """query userAcademicProfile($uid: String) {
  user(uid: $uid) {
    gpa { errored role cumulativeGPA description }
    units { errored totalUnits totalUnitsAttempted totalUnitsForGPA totalUnitsPassedNotForGPA
            totalTransferAndTestingUnits transferUnitsAccepted testingUnits }
    majors { description formalDescription college }
    minors { description formalDescription college }
    latestRegistrationTerm { id description }
    termsInAttendance
    hasHolds
  }
}"""


def _allowed(path: str) -> bool:
    bare = path.split("?", 1)[0]
    return any(r.fullmatch(bare) for r in _READ_RE)


def _get(path: str, params=None):
    if not _allowed(path):
        raise PermissionError(f"{path} is not on the read-only allow-list (calc.READ_OK)")
    return _cc.get(path, params)


def term_name(code) -> str:
    """Berkeley term code -> name: 2272 = Spring 2027, 2268 = Fall 2026, 2265 = Summer 2026."""
    s = str(code or "")
    if not re.fullmatch(r"2\d{3}", s):
        return s
    season = {"2": "Spring", "5": "Summer", "8": "Fall"}.get(s[3], "?")
    return f"{season} 20{s[1:3]}"


def _match_term(want: str | None, *names) -> bool:
    if not want:
        return True
    w = " ".join(want.lower().split())
    return any(n and w in str(n).lower() for n in names)


def _pick(d: dict, *keys):
    return {k: d[k] for k in keys if isinstance(d, dict) and d.get(k) not in (None, "", [], {})}


# ---------------- tools ----------------

def status() -> dict:
    try:
        _cc._checked_at = 0
        _cc.ensure()
        saved = json.loads(COOKIES.read_text()).get("saved_at")
        return {"signed_in": True, "session_saved": time.strftime("%Y-%m-%d %H:%M", time.localtime(saved))}
    except SignInRequired as e:
        return {"signed_in": False, "how_to_sign_in": str(e)}


def raw_get(path: str):
    if not path.startswith("/"):
        path = "/" + path
    data = _get(path)
    text = json.dumps(data, ensure_ascii=False)
    if len(text) > MAX_RAW:
        return {"path": path, "truncated": True, "chars": len(text), "json_start": text[:MAX_RAW]}
    return data


def holds():
    return _get("/api/my/holds")


def _schedules(sec: dict) -> list:
    sch = sec.get("schedules") or {}
    out = []
    if isinstance(sch, dict):
        for kind in ("recurring", "oneTime"):
            for s in sch.get(kind) or []:
                out.append(_pick(s, "schedule", "buildingName", "roomNumber", "date", "time"))
    elif isinstance(sch, list):
        out = [_pick(s, "schedule", "buildingName", "roomNumber", "dayOfTheWeek", "startTime", "endTime") for s in sch]
    return [x for x in out if x]


def _section(sec: dict) -> dict:
    d = _pick(sec, "section_label", "ccn", "is_primary_section", "units", "grading_basis", "gradeOption",
              "enroll_status", "enroll_status_code", "waitlisted", "waitlistPosition", "enroll_limit",
              "hasReservedSeats", "uc_reason_desc", "start_date", "end_date")
    if sec.get("instructors"):
        d["instructors"] = [i.get("name") for i in sec["instructors"] if isinstance(i, dict) and i.get("name")]
    when = _schedules(sec)
    if when:
        d["meets"] = when
    d["status"] = "waitlisted" if (sec.get("waitlisted") or str(sec.get("enroll_status", "")).upper() == "W") \
        else "enrolled"
    return d


def schedule(term: str | None = None) -> dict:
    acad = _get("/api/my/academics")
    out = []
    for sem in acad.get("semesters") or []:
        name = sem.get("name") or term_name(sem.get("termId"))
        if not _match_term(term, name, sem.get("slug"), term_name(sem.get("termId"))):
            continue
        classes = []
        for c in sem.get("classes") or []:
            secs = [_section(s) for s in c.get("sections") or []]
            classes.append({
                **_pick(c, "course_code", "title", "dept", "courseCatalog", "role", "courseCareerCode", "url"),
                "units": sum(float(s.get("units") or 0) for s in secs if s.get("is_primary_section")) or None,
                "status": "waitlisted" if any(s["status"] == "waitlisted" and s.get("is_primary_section") for s in secs)
                          else "enrolled",
                "sections": secs,
            })
        enrolled_units = sum(c["units"] or 0 for c in classes if c["status"] == "enrolled")
        waitlist_units = sum(c["units"] or 0 for c in classes if c["status"] == "waitlisted")
        out.append({"term": name, "term_id": sem.get("termId"), "time_bucket": sem.get("timeBucket"),
                    "enrolled_units": enrolled_units, "waitlisted_units": waitlist_units, "classes": classes})
    if not out:
        return {"note": f"no semesters matched {term!r}", "terms_available":
                [s.get("name") for s in acad.get("semesters") or []]}
    return {"source": "/api/my/academics", "terms": out}


def enrollment(term: str | None = None) -> dict:
    res = _get("/api/my/enrollment_resources")
    terms = []
    for ti in res.get("termInstructions") or []:
        tid = ti.get("term_id") or ti.get("termId")
        name = term_name(tid)
        if not _match_term(term, name, ti.get("term_descr"), ti.get("termName")):
            continue
        periods = [{"period": p.get("name"), "career": p.get("acadCareer"),
                    "begins": (p.get("beginTime") or {}).get("pacific"),
                    "ends": (p.get("endTime") or {}).get("pacific")} for p in ti.get("enrollmentPeriods") or []]
        extra = {k: v for k, v in ti.items() if k not in ("enrollmentPeriods", "term_id", "termId")}
        terms.append({"term": name, "term_id": tid, "appointments": periods, **extra})
    msgs = res.get("messages") or {}
    return {
        "source": "/api/my/enrollment_resources",
        "has_holds": res.get("hasHolds"),
        "terms": terms,
        "presentations": res.get("enrollmentPresentations"),  # unit caps / deadlines when CalCentral sends them
        "messages": {k: (v.get("descrlong") or v.get("descr") if isinstance(v, dict) else v) for k, v in msgs.items()},
        "note": "Appointment times are the user's own. Class-wide phase windows: class_seats. "
                "This server can't enroll, drop or edit the cart.",
    }


def billing() -> dict:
    out = {}
    for key, path in (("billing_items", "/api/my/finances/billing_items"),
                      ("billing_activity", "/api/campus_solutions/billing_activity"),
                      ("payment_plan", "/api/campus_solutions/fpp_enrollment")):
        try:
            out[key] = _get(path)
        except SignInRequired:
            raise
        except Exception as e:
            out[key] = {"error": f"{type(e).__name__}: {e}"}
    out["note"] = "Read-only. Paying stays the user's click in CalCentral."
    return out


def _gql(query: str, variables: dict | None = None) -> dict:
    """POST one fixed read-only GraphQL query. CSRF token comes from /api/my/status when CalCentral provides it."""
    if query is not GQL_PROFILE:
        raise PermissionError("only the built-in read-only GraphQL query is allowed")
    _cc.ensure()
    st = _cc.client().get("/api/my/status").json()
    token = st.get("csrfToken") or st.get("csrf_token") or ""
    r = _cc.client().post("/graphql", json={"query": query, "variables": variables or {}},
                          headers={"Content-Type": "application/json", "X-CSRF-Token": token})
    r.raise_for_status()
    return r.json()


def grades() -> dict:
    acad = _get("/api/my/academics")
    terms = []
    for sem in acad.get("semesters") or []:
        rows = []
        for c in sem.get("classes") or []:
            for t in c.get("transcript") or []:
                rows.append({"course": c.get("course_code"), "title": c.get("title"),
                             **_pick(t, "units", "grade", "grading_basis", "gradePoints")})
        if rows:
            terms.append({"term": sem.get("name") or term_name(sem.get("termId")), "grades": rows})
    out = {"source": "/api/my/academics (transcript rows)", "terms": terms}
    for k in ("gpaUnits", "gpa", "cumulativeGpa"):
        if acad.get(k):
            out[k] = acad[k]
    try:
        prof = (_gql(GQL_PROFILE).get("data") or {}).get("user") or {}
        out["profile"] = {"gpa": prof.get("gpa"), "units": prof.get("units"), "majors": prof.get("majors"),
                          "minors": prof.get("minors"), "terms_in_attendance": prof.get("termsInAttendance")}
    except SignInRequired:
        raise
    except Exception as e:
        out["profile_error"] = f"{type(e).__name__}: {e}"
    out["note"] = "From CalCentral JSON, not the rendered page."
    return out


def degree_progress():
    return _get("/api/academics/degree_progress/ugrd")
