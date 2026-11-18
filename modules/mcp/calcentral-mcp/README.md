# calcentral-mcp

A local, **read-only** MCP server for your own record in CalCentral (the Berkeley student portal) and the public class schedule. Built on 2026-11-16, to replace in-app-browser sessions on CalCentral and hand-written JS seat checks.

## Tools

**CalCentral: the user's own record. Needs their sign-in.**

| Tool | What it returns | Feed |
|---|---|---|
| `calcentral_status` | Whether the saved session is still signed in | `/api/my/status` |
| `my_schedule(term)` | Enrolled and waitlisted classes, units, times, rooms, waitlist position | `/api/my/academics` |
| `enrollment_info(term)` | Their Phase 1 / Phase 2 / Adjustment appointment times, unit caps and deadlines when sent | `/api/my/enrollment_resources` |
| `my_holds` | Holds on their record | `/api/my/holds` |
| `my_billing` | Charges, payments, due dates and the tuition payment plan | `/api/my/finances/billing_items`, `/api/campus_solutions/billing_activity`, `/api/campus_solutions/fpp_enrollment` |
| `my_grades` | Grades by term, plus GPA and units | `/api/my/academics` transcript rows, plus the GraphQL query `userAcademicProfile` |
| `degree_progress` | Degree requirements status | `/api/academics/degree_progress/ugrd` |
| `calcentral_get(path)` | Raw JSON from any other read feed on the allow-list | `calc.READ_OK` |

**Public: no login.**

| Tool | What it returns | Source |
|---|---|---|
| `class_seats(course, term, section, kind)` | Enrolled out of capacity, open seats, waitlist count and cap, reserved seats, meetings | classes.berkeley.edu section page (its embedded JSON) |
| `class_search(query, term, filters, page)` | Sections with times, rooms and seat summary. Filters look like `breadth_requirements:Biological Science` | classes.berkeley.edu search |
| `grade_distribution(course, instructor)` | Average GPA, percent of A-range grades, letter counts | Berkeleytime GraphQL |

## What it will never do

The server has **no** tools to enroll, drop, swap, edit the shopping cart, accept an agreement, or pay. Every CalCentral request is one of two things:

- a GET to a path that matches `calc.READ_OK`, or
- the one fixed GraphQL **query** in `calc.GQL_PROFILE`, which is never built from tool input and is never a mutation.

The user's rule stands: nothing is enrolled, dropped or paid without asking them item by item in chat, and those clicks stay theirs.

GPA comes from CalCentral's JSON. It is never scraped from the rendered page, which has shown a wrong GPA before.

## Sign-in (the user does it themselves)

```bash
sh ~/code/calcentral-mcp/signin.sh          # add --tour to also log which /api/ feeds the pages load
```

1. The script opens a Chrome window on a **private profile** at `~/.local/share/calcentral-mcp/profile`. It is separate from their everyday Chrome.
2. They click Sign In and do CalNet + Duo there.
3. The script waits until `/api/my/status` says `isLoggedIn`, then saves the cookies to `~/.local/share/calcentral-mcp/cookies.json` (chmod 600, folder chmod 700) and closes the window.

The script never types into a page, and nothing here sees the password.

**At run time** the server sends plain HTTPS requests with those cookies, so no browser is involved. When CalCentral says the session is over, the server tries **one** silent refresh: a headless Chrome on the same profile reopens `/auth/cas`. If CalNet still remembers them (its single sign-on cookie), a new session comes back with no password. If CalNet shows its login page or Duo, the tool returns:

> SIGN-IN NEEDED: ... The user runs `sh ~/code/calcentral-mcp/signin.sh` ...

Claude can start that script with Bash in the background, but the user signs in. Claude never fills in CalNet.

## Class data etiquette

classes.berkeley.edu rate-limits: plain curl got a 403 and parallel requests got a 429 on 2026-11-03. So the server:

- sends one request at a time, at least 3 s apart, even across processes (a lock plus timestamp file at `~/.local/share/calcentral-mcp/classes-throttle`);
- caches pages for 10 minutes (`cache/`);
- backs off once on a 429 or 403, then reports the error;
- sends a normal browser User-Agent.

Term ids (Spring 2027 = 8589) are learned from the site's facets and kept in `terms.json`.

Seat counts come from the page's own JSON at `ucb.enrollment.available`, and they match the "Current Enrollment" box on the page. The phase windows the site shows are class-wide. Their personal appointment times come from `enrollment_info`.

## Files

- `server.py`: the FastMCP tools.
- `calc.py`: CalCentral feeds and the allow-list.
- `session.py`: cookies, silent refresh and the browser lock.
- `classes.py`: class schedule and Berkeleytime.
- `signin.py` and `signin.sh`: the user's sign-in window.

**Environment:** `.venv` holds Python 3.12 with mcp 1.30.0 (keep it under 2), httpx 0.28.1 and playwright 1.65.2. The browser is the installed Google Chrome (`channel="chrome"`), so there is no extra download.

**Startup:** about 1 s. Playwright is imported only for a silent refresh.

## Registration and bridge

```bash
claude mcp add --scope user calcentral -- ~/code/calcentral-mcp/.venv/bin/python ~/code/calcentral-mcp/server.py
python3 ~/code/scripts/mcp_bridge.py calcentral call my_schedule '{"term":"Spring 2027"}'
python3 ~/code/scripts/mcp_bridge.py calcentral call class_seats '{"course":"MATH 1A"}'
```

Remove it with `claude mcp remove calcentral -s user`.

## Rebuild

```bash
cd ~/code/calcentral-mcp
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python "mcp==1.30.0" "httpx==0.28.1" "playwright==1.65.2"
```

No `playwright install` is needed, because the server uses the installed Chrome.

## How the feeds were found

The CalCentral source repo is private now. The feed list came from its public front-end bundle (`https://calcentral.berkeley.edu/main.<hash>.js`). To refresh it after a redesign, grep the bundle for `"/api/` and for `query ` (its GraphQL operations), or run `signin.sh --tour` and read `~/.local/share/calcentral-mcp/api_calls.json`. That file holds URLs only.

The PeopleSoft Enrollment Center (`bcsweb.is.berkeley.edu`) is not used. The CalCentral feeds should cover the schedule. Confirm that at the first signed-in test, and add PeopleSoft reads only if something is missing.
