"""New grades from Gradescope (coursework connector) and Canvas (Canvas API).

A grade is keyed by assignment + score, so a regrade is a new key in the same group (pushed as
"Updated: ..."). If one exam shows on both sites with the same score and a similar name, only the
Gradescope line is pushed; the Canvas one is returned silent (text "") so it is marked seen.
If either site fails, the whole fetch fails: skipping one would make all its old grades look new
when it comes back.
"""
import re

from . import bridge, canvas, register


def _num(x) -> str:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return str(x)
    return str(int(f)) if f == int(f) else f"{f:g}"


def _words(name: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if len(w) >= 3 or w.isdigit()}


def _gradescope(params) -> list:
    res = bridge.call("coursework", "gradescope_assignments", {"include_past": True})
    out = []
    for a in res.get("assignments", []):
        if a.get("grade") is None:
            continue
        aid, g = a["assignment_id"], _num(a["grade"])
        mx = f" / {_num(a['max_grade'])}" if a.get("max_grade") is not None else ""
        out.append({"key": f"gs:{aid}:{g}", "group": f"gs:{aid}",
                    "text": f"{a.get('course', '')}: '{a.get('assignment', '')}' graded, {g}{mx}",
                    "_course": a.get("course"), "_score": g, "_name": a.get("assignment")})
    return out


def _canvas(params) -> list:
    out = []
    for cid, label in (params.get("canvas_courses") or {}).items():
        subs = canvas.canvas_get(f"/courses/{cid}/students/submissions",
                                 [("student_ids[]", "self"), ("include[]", "assignment"), ("per_page", 100)])
        for s in subs or []:
            if s.get("score") is None:
                continue
            asg = s.get("assignment") or {}
            aid, sc = s["assignment_id"], _num(s["score"])
            mx = f" / {_num(asg['points_possible'])}" if asg.get("points_possible") is not None else ""
            out.append({"key": f"cv:{aid}:{sc}", "group": f"cv:{aid}",
                        "text": f"{label}: '{asg.get('name', '')}' graded, {sc}{mx}",
                        "_cid": str(cid), "_score": sc, "_name": asg.get("name")})
    return out


@register("grades")
def fetch(params: dict, ctx) -> list:
    gs = _gradescope(params) if params.get("gradescope") else []
    cv = _canvas(params)
    cmap = params.get("gs_course_map") or {}
    for c in cv:
        for g in gs:
            if (cmap.get(g["_course"]) == c["_cid"] and g["_score"] == c["_score"]
                    and len(_words(g["_name"]) & _words(c["_name"])) >= 2):
                c["text"] = ""
                break
    return [{k: v for k, v in i.items() if not k.startswith("_")} for i in gs + cv]
