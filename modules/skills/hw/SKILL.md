---
name: hw
description: Start a separate Claude session for one homework assignment, launched from the semester-tracker session. Gathers the assignment (due date, instructions, files, AI-use rules), then creates a one-click "start session" chip whose prompt carries the full brief. Use when the user says "/hw <assignment>", "start <assignment> in its own session", "spin up a session for the bio homework", or similar.
argument-hint: "<assignment, e.g. 'CS 101 homework 4'>"
---

# Start a homework session

Assignment: **$ARGUMENTS**

The tracker session stays the planner. Each assignment gets its own session, so the homework context never crowds out the to-do list. (Why: a long homework conversation fills the context window, and the planner then forgets deadlines.)

## 1. Gather the brief (read-only)

Do these from this session:

- **Tracker:** find the row and its due date in the merged deadlines:
  `cd ~/semester-tracker/verify && python3 -c "from deadlines_lib import *; import sys; rows,_=merge_rows(load(PATHS['tracker']),load(PATHS['heartbeat'])); q=sys.argv[1].lower(); [print(r['id'], r['when'], 'DONE' if r.get('done') else 'open', r['label'][:140]) for r in rows if q in r['label'].lower()]" "<keyword>"`
  Also check `~/semester-tracker/schedule.md` for the item.
- **Instructions:** read them from the course folder (`~/semester-tracker/<course>/`: `materials/`, `readings/`, `notes/COURSE.md`). If they aren't saved locally, get the assignment through the `canvas` MCP (get_assignment_details, get_page_content; save attached files with download_course_file into the course folder), and school Drive/Docs through the `google-workspace` MCP. Use a browser only if those can't reach it. Note the rubric, the format (handwritten? upload type?), and the late policy.
- **AI-use rule (required):**
  - Check the syllabus and assignment text for an AI policy.
  - **First check `~/semester-tracker/EXCEPTIONS.md`.** An exception there (an instructor or TA allowed something different) overrides the printed syllabus. If the user says now that an instructor approved something, log it there first (see the "Course rules" section of CLAUDE.md), then use it.
  - When a no-AI rule applies, the new session is a **tutor only**. It explains concepts, quizzes the user, and checks their understanding. It never writes the graded work.
  - When unsure, say so in the brief and default to tutor-only.

## 2. Create the session chip

Use the desktop app's session tool (`mcp__ccd_session__start_session`, or `spawn_task` on older builds) with:

- **title:** `<COURSE> <assignment> — due <Mon 10/12>`. Keep it under 60 characters.
- **cwd:** `~/semester-tracker` (or the course folder).
- **prompt:** a self-contained brief with these parts:
  1. The assignment: what to submit, where (Canvas/Gradescope), the exact due date and time, the rubric points, and the instructions or a path to them.
  2. The files to read first: exact paths.
  3. The AI-use rule, stated plainly. If it's tutor-only, say what the session may and may not do.
  4. Where to save work: `~/semester-tracker/<course>/hw/<slug>/`. Commit on **main only, never a branch** (a branch checkout can hide files that only exist on main), then push.
  5. Finishing:
     - Once the user confirms they submitted, run `python3 verify/mark_done.py "<tracker id>"` in `~/semester-tracker`.
     - Commit and push both the tracker and the heartbeat repo.
     - Never edit deadlines.json any other way.
  6. Read-only rules: school mail and accounts are read-only. Don't sign in to anything for the user. Don't submit anything on their behalf.

## 3. Tell the user

Write one short line covering:
- "Click the chip to start it."
- Whether the session is tutor-only.

Offer to file it under a sidebar group for this term's classes (for example "Classes"), using `mcp__ccd_session_mgmt__list_sessions` to find the new session and `mcp__ccd_sidebar__move_sessions` to move it.
