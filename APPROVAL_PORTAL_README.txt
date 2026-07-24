APPROVAL PORTAL — Clickable HITL Decisions
=============================================

WHAT THIS IS
-------------
A small local web server that turns Manual Review into a clickable
experience: instead of typing APPROVE/REJECT in a terminal, the
reviewer opens a link from their email and clicks a button.

The review-request email now contains:
  - "📋 Open Review Page" — shows full invoice details + reasons for
    review, lets the reviewer type their name and comments, then pick
    Approve / Reject / Request Clarification.
  - Three one-click links (Approve / Reject / Request Clarification)
    that record the decision immediately as "AP Reviewer" with no
    comments — useful for a fast yes/no when no commentary is needed.

HOW TO START IT
-----------------
From the ap_desktop_v2 directory:

    pip install -r requirements.txt      (adds fastapi + uvicorn)
    python approval_portal.py

You should see:

    🌐 Starting AP Approval Portal on http://0.0.0.0:8000
       Pending reviews: http://localhost:8000/pending-reviews

Leave this running in its own terminal window/process. It needs to be
up whenever a reviewer might click a link — start it before (or at the
same time as) run_ap.py.

HOW TO USE IT WITH run_ap.py
-------------------------------
    python run_ap.py --hitl-mode web              (live Outlook mode)
    python run_ap.py --mock --hitl-mode web        (quick test, no Outlook)
    python run_ap.py --watch --hitl-mode web        (continuous polling mode)

When an invoice needs Manual Review under "web" mode, run_ap.py does
NOT wait for a decision — it sends the review email and immediately
moves on to the next invoice (or, in --mock, finishes that run). The
invoice sits in a "Pending Web Review" state until someone clicks a
link, at which point the portal finishes the pipeline (sends the
result email, logs the invoice, moves the source email if Outlook is
available).

CONFIGURATION (.env)
----------------------
    AP_APPROVAL_PORTAL_URL=http://localhost:8000
    AP_APPROVAL_PORTAL_PORT=8000

Change AP_APPROVAL_PORTAL_URL if reviewers are on a different machine
than the one running the portal — it must be a URL their browser can
actually reach (e.g. your machine's LAN IP or a proper hostname, NOT
"localhost" unless they're on the same machine).

WHERE PENDING REVIEWS LIVE
-----------------------------
    data/pending_reviews/{token}.json

One file per invoice currently awaiting a decision. Deleted
automatically once resolved. Safe to inspect/delete manually if you
need to clear a stuck review (e.g. testing) — the corresponding link
will then 404 cleanly.

CHECKING WHAT'S PENDING
--------------------------
    http://localhost:8000/pending-reviews

Returns a JSON list of every invoice currently awaiting a decision,
with a direct link to each review page.

TESTING WITHOUT A BROWSER OR OUTLOOK
---------------------------------------
    python test_web_hitl_portal.py

Exercises the entire flow — review page rendering, the JSON API, the
one-click links, validation, and reviewer-comment handling — using
FastAPI's TestClient (an in-process fake HTTP client). Safe to run
repeatedly; it cleans up its own pending-review records.

TROUBLESHOOTING "THE LINK DOESN'T WORK"
-------------------------------------------
This is almost always one of three things:

1. THE PORTAL ISN'T RUNNING.
   Symptom: clicking the link shows "This site can't be reached" /
   "connection refused" in the browser.
   Fix: run `python approval_portal.py` and leave it running. As of
   this version, `run_ap.py --hitl-mode web` now checks portal
   reachability at startup and prints a clear ❌ error if it can't
   connect — watch for that before assuming an invoice was sent.

2. .env WASN'T FOUND (fixed in this version).
   Previously, .env was loaded relative to the current working
   directory — if you launched run_ap.py from anywhere other than the
   ap_desktop_v2 folder (a different PowerShell location, a scheduled
   task, etc.), .env was silently never found and every setting fell
   back to its hardcoded default with NO warning. This is now fixed:
   .env is located relative to each script's own file location, so it
   loads correctly no matter where you run the command from.

3. "localhost" DOESN'T MEAN WHAT YOU THINK IT MEANS.
   If AP_APPROVAL_PORTAL_URL is http://localhost:8000 (the default),
   that link ONLY works if the reviewer opens the email on the EXACT
   SAME machine that's running approval_portal.py. If your reviewer
   uses a different PC — which is the normal case — clicking the link
   tries to reach a server on THEIR OWN machine, finds nothing, and
   fails every time with no useful error message in the email itself.

   Fix: find this machine's real network address and put that in
   .env instead of localhost:

       Windows:  ipconfig                  (look for IPv4 Address)
       Then set: AP_APPROVAL_PORTAL_URL=http://<that-IP>:8000

   Both run_ap.py and approval_portal.py now print a ⚠️ warning at
   startup if AP_APPROVAL_PORTAL_URL still contains "localhost", so
   you'll see this reminder every time you start either process.

ARCHITECTURE NOTE
--------------------
ap_desktop_v2 has no database — pending reviews are plain JSON files
(see pending_reviews.py), and the "resume" logic lives in
run_ap.resume_after_review(), called by approval_portal.py once a
decision arrives. This mirrors the synchronous CLI/email-only HITL
paths in process_invoice_set() but runs later, triggered by an HTTP
request instead of a terminal prompt — both paths share the exact same
finishing logic (_finalize_decision() in run_ap.py) so results are
identical regardless of how the reviewer responded.
