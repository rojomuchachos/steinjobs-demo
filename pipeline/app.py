"""`make app` — the pipeline as a local web app.

"I want to eventually run everything from the app." A static dashboard can't
run anything, so this is the smallest server that makes the buttons real:
Python stdlib http.server, bound to 127.0.0.1 only, no dependencies, no
accounts, nothing exposed. It serves the dashboard (rebuilt fresh on every
page load, so it can never be stale) and a handful of actions:

    POST /api/scout      run a scout in the background (one at a time)
    POST /api/mark       {url, status, note}
    POST /api/note       {url, note}
    POST /api/track      {company, why}        — track a company by hand
    POST /api/person     {name, company, ...}  — add a networking contact
    POST /api/person-status {name, company, status} — advance an outreach stage
    POST /api/tweet-status {url, status}       — new / saved / dismissed an X lead
    POST /api/company-interest {company, interested} — dismiss / restore a company
    POST /api/feedback   {url, judgment, note} — "why this score" feedback → calibration
    POST /api/queue-founder {company}          — add to the founder-lookup queue
    GET  /api/row?url=…                        — one row's job description
    GET  /api/ping       lets the page detect it's live vs. a static file

Actions call the exact same functions the CLI does — same validation, same
history, same calibration side-effects. The browser is another front door,
not another code path. Anything the server can't do (send email, apply)
doesn't exist here either.
"""

from __future__ import annotations

import json
import threading
import webbrowser
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


from . import dashboard, entities, feed
from .enrich import _discover_site, _formd_stage, _guess_industry
from . import history as hist
from . import status as st
from .models import today
from .brief import APPLICATIONS

from pathlib import Path as _Path

AUTOFILL_LOG = _Path(__file__).resolve().parent.parent / "data" / "autofill_log.json"


def _log_autofill(kind: str, name: str) -> None:
    """One line per button-triggered autofill, so 'Recently autofilled' in the
    Queue panel reflects manual runs too, not only the Chrome sittings."""
    import datetime as _dt
    import json as _json
    try:
        log = _json.loads(AUTOFILL_LOG.read_text()) if AUTOFILL_LOG.exists() else []
    except ValueError:
        log = []
    log.insert(0, {"at": _dt.datetime.now().isoformat(timespec="minutes"),
                   "kind": kind, "name": name})
    AUTOFILL_LOG.write_text(_json.dumps(log[:40], indent=2))


# Loopback by DEFAULT and on purpose: this server has no auth and its POST
# routes mutate the feed, the overlays and the queues. Anyone who can reach the
# port can change Eric's pipeline.
#
# JOBS_HOST overrides it so a phone on the same wifi can read the app (Eric,
# 2026-08-14 — he was away from the machine and AirDrop wasn't an option).
# Use it on a network you trust; on shared wifi, don't. serve() prints a loud
# warning whenever the bind is not loopback so this can't happen by accident.
HOST = os.environ.get("JOBS_HOST", "127.0.0.1")
PORT = int(os.environ.get("JOBS_PORT", "8377"))

_scout_state = {"running": False, "last": "", "log": [], "boards_total": 0, "boards_done": 0}







class _ScoutLog:
    """File-like sink that turns the scout's own prints into live progress.

    The CLI already narrates itself ("fetching waas…", "scored via…"); instead
    of burying that in a buffer, each line lands in _scout_state where the page
    polls it. Boards done is counted off the "fetching" lines, which is what
    makes the progress bar honest rather than a spinner."""

    def write(self, text: str) -> int:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            _scout_state["log"] = (_scout_state["log"] + [line])[-12:]
            if line.startswith("fetching "):
                _scout_state["boards_done"] += 1
        return len(text)

    def flush(self) -> None:  # file-protocol requirement
        pass


def _run_scout() -> None:
    try:
        from contextlib import redirect_stderr, redirect_stdout

        import yaml

        from .cli import BOARDS_PATH
        from .cli import main as cli_main

        boards = yaml.safe_load(BOARDS_PATH.read_text())["boards"]
        _scout_state.update(log=[], boards_done=0,
                            boards_total=sum(1 for b in boards.values()
                                             if b.get("tier") != "skip"))
        sink = _ScoutLog()
        with redirect_stdout(sink), redirect_stderr(sink):
            cli_main(["scout"])
        _scout_state["last"] = f"scout finished {today()}"
    except Exception as exc:  # noqa: BLE001
        _scout_state["last"] = f"scout failed: {type(exc).__name__}"
    finally:
        _scout_state["running"] = False


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict | bytes, ctype: str = "application/json") -> None:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        # Same-origin only; the page is served from this same server.
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except json.JSONDecodeError:
            return {}

    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        if self.path.startswith("/sfx/"):
            from . import sfx
            name = self.path[5:].split("?")[0]
            if name in [n + ".wav" for n in sfx.NAMES]:
                self._send(200, (sfx.ensure() / name).read_bytes(), "audio/wav")
            else:
                self._send(404, {"error": "not found"})
        elif self.path.split("?")[0] in ("/", "/index.html"):
            page = dashboard.build(feed.load())  # always fresh, never stale
            self._send(200, page.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/ping":
            self._send(200, {"ok": True, "scout": _scout_state})
        elif self.path == "/api/data":
            # The soft-reload backbone: the freshly built page's payload as
            # JSON, so the client re-renders in place with no flash.
            import re as _re

            dashboard.build(feed.load())
            html_txt = dashboard.OUT.read_text(encoding="utf-8")
            m = _re.search(r"^let DATA = (.*);$", html_txt, _re.M)
            if not m:
                self._send(500, {"error": "payload marker not found"})
            else:
                self._send(200, m.group(1).encode("utf-8"), "application/json")
        elif self.path.split("?")[0] == "/api/row":
            # Descriptions are 212KB across the feed and only read on expand,
            # so they're fetched per row rather than inlined into the page.
            from urllib.parse import parse_qs, urlparse

            from . import store
            from .dashboard import _desc_snippet

            url = (parse_qs(urlparse(self.path).query).get("url") or [""])[0]
            self._send(200, {"ok": True, "desc": _desc_snippet(url, store.load())})
        elif self.path == "/api/insights":
            # Structured, not prose — the panel renders sections, and the
            # CLI keeps its own text renderer for `make insights`.
            from . import importer, insights

            entries = feed.load()
            ag = insights.agreement(entries)
            oo = insights.outreach_outcomes(entries)
            self._send(200, {"ok": True, "data": {
                "agreement": {
                    "n_pos": ag["n_pos"], "n_neg": ag["n_neg"],
                    "mean_pos": round(ag["mean_pos"]) if ag["mean_pos"] is not None else None,
                    "mean_neg": round(ag["mean_neg"]) if ag["mean_neg"] is not None else None,
                    "false_high": [{"t": e.title, "c": e.company, "s": e.score, "u": e.url}
                                   for e in ag["false_high"]],
                    "false_low": [{"t": e.title, "c": e.company, "s": e.score, "u": e.url}
                                  for e in ag["false_low"]],
                },
                "reasons": insights.pass_reasons(importer.load_calibration()),
                "outreach": {"sent": dict(oo["sent"]), "replied": dict(oo["replied"])},
            }})
        elif self.path.startswith("/applications/"):
            # Serve artifacts (dossier, brief) so chips are links.
            # Resolve inside the applications dir only — no traversal.
            rel = self.path[len("/applications/"):]
            target = (APPLICATIONS / rel).resolve()
            if not str(target).startswith(str(APPLICATIONS.resolve())) or not target.is_file():
                self._send(404, {"error": "not found"})
                return
            ctype = {
                ".pdf": "application/pdf", ".html": "text/html; charset=utf-8",
                ".md": "text/plain; charset=utf-8", ".js": "text/javascript",
            }.get(target.suffix, "application/octet-stream")
            self._send(200, target.read_bytes(), ctype)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        body = self._body()
        try:
            if self.path == "/api/scout":
                if _scout_state["running"]:
                    self._send(409, {"error": "a scout is already running"})
                    return
                # Find New Roles covers every headless board; Wellfound needs a
                # real Chrome, so the button arms the next scheduled sitting to
                # sweep it — the closest thing to "one button, all boards".
                import json as _json

                from .models import today as _today
                qp = _Path(__file__).resolve().parent.parent / "data" / "board_sweep_queue.json"
                try:
                    q = _json.loads(qp.read_text()) if qp.exists() else []
                except ValueError:
                    q = []
                if not any(x.get("board") == "wellfound" for x in q):
                    q.append({"board": "wellfound", "queued_at": _today(),
                              "via": "find-new-roles"})
                    qp.write_text(_json.dumps(q, indent=2))
                _scout_state["running"] = True
                threading.Thread(target=_run_scout, daemon=True).start()
                self._send(202, {"ok": True,
                                 "note": "scout started — Wellfound queued for the next Chrome sitting"})

            elif self.path == "/api/mark":
                entries = feed.load()
                # Capture from-states BEFORE mark mutates them — undo depends on
                # this, and the CLI path already records it. The browser must be
                # another front door, not a lossier one.
                probe = st.find(entries, body.get("url", ""))
                prior = [e.status for e in probe]
                changed, msg = st.mark(
                    entries, body.get("url", ""), body.get("status", ""), body.get("note", "")
                )
                if not changed:
                    self._send(400, {"error": msg})
                    return
                h = hist.load()
                when = body.get("date", "")
                for e, was in zip(changed, prior):
                    if when:
                        e.last_touched = when
                    detail = f"from {was}" + (f" — {body['note']}" if body.get("note") else "")
                    h.add(e.url, "status", f"moved to {e.status}", detail, at=when)
                hist.save(h)
                feed.save(entries)
                # Interest cascades down AND up: saving a posting means the
                # company matters — follow it and queue its alumni sweep.
                # An explicit company-level "uninterested" is never overridden.
                if body.get("status") in ("saved", "applied", "interviewing", "offer"):
                    for e in changed:
                        if not e.company:
                            continue
                        from .models import normalize_company as _nc
                        rec = entities.load_company_overlay().get(_nc(e.company), {})
                        if rec.get("not_interested"):
                            continue
                        if not rec.get("tracked"):
                            entities.set_company_mode(e.company, "saved")
                            h.add(e.url, "note", f"auto-saved company {e.company}",
                                  "a posting there was saved", at="")
                    hist.save(h)
                if body.get("note"):
                    from . import importer

                    cal = importer.load_calibration()
                    for e in changed:
                        cal.append(st.calibration_record(e, body["note"]))
                    importer.save_calibration(cal)
                self._send(200, {"ok": True, "msg": msg})

            elif self.path == "/api/note":
                entries = feed.load()
                hits = st.find(entries, body.get("url", ""))
                if not hits:
                    self._send(400, {"error": "no entry matched"})
                    return
                e = hits[0]
                hist.record(e.url, "note", body.get("note", ""))
                e.status_note = body.get("note", "")
                e.last_touched = today()
                feed.save(entries)
                self._send(200, {"ok": True})

            elif self.path == "/api/add-posting":
                # Paste-a-link entry: dedupe against the ledger, best-effort
                # title/company from the page itself, never invented.
                from .models import Entry, normalize_url, today as _today

                url = (body.get("url") or "").strip()
                title = (body.get("title") or "").strip()
                company = (body.get("company") or "").strip()
                if not url:
                    # Off-market roles — heard from a founder, never posted —
                    # have no URL. The ledger dedupes by url and every UI
                    # action keys on it, so it must never be blank: synth a
                    # stable internal key from what identifies the role.
                    if not (title and company):
                        self._send(400, {"error": "no URL? give both a title and a company instead"})
                        return
                    import re as _re

                    def _slug(s: str) -> str:
                        return _re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")

                    url = f"manual://{_slug(company)}/{_slug(title)}"
                elif not url.startswith("http"):
                    self._send(400, {"error": "paste a full http(s) job posting URL"})
                    return
                entries = feed.load()
                idx = feed.Index(entries)
                dup = idx.by_url.get(normalize_url(url))
                if dup:
                    self._send(409, {"error": f"already in the feed: {dup.company} — {dup.title}"})
                    return
                # Browser sweeps (Wellfound etc.) POST full fields; the
                # paste-a-link path supplies only a URL and gets best-effort
                # title scraping. Supplied fields always win — the sweep read
                # them off the real page, which beats a blocked refetch.
                if not title and url.startswith("http"):
                    try:
                        from .boards.base import client
                        import re as _re

                        with client() as c:
                            r = c.get(url, headers={"Accept": "text/html"})
                            if r.status_code == 200:
                                m = _re.search(r"<title[^>]*>([^<]{3,180})</title>", r.text, _re.I)
                                if m:
                                    import html as _h
                                    title = _h.unescape(m.group(1)).strip()
                                    parts = _re.split(r"\s+[|\u2013\u2014-]\s+", title)
                                    if len(parts) >= 2:
                                        title = parts[0].strip()
                                        company = company or parts[-1].strip()
                    except Exception:  # noqa: BLE001 — the link alone is enough
                        pass
                desc = (body.get("description") or "").strip()
                if desc:
                    from . import store
                    cache = store.load()
                    cache[normalize_url(url)] = desc
                    store.save(cache)
                e = Entry(title=title or "(untitled — open the link)",
                          company=company,
                          url=url, source=body.get("source") or "manual",
                          location=(body.get("location") or "").strip(),
                          score=None, why="added by hand",
                          date_added=_today(), first_seen=_today(),
                          # Hand-added = already chosen: straight to saved
                          # (Eric, 2026-08-18). Sweep POSTs carry a source
                          # and still land in review for scoring/triage.
                          status="saved" if (body.get("source") or "manual") == "manual"
                                 else "review")
                entries.append(e)
                feed.save(entries)
                hist.record(url, "note", "added by hand", at="")
                self._send(200, {"ok": True, "title": e.title, "company": e.company})

            elif self.path == "/api/track":
                rec = entities.track_company(
                    body.get("company", ""), body.get("why", ""), body.get("site", ""),
                    linkedin=body.get("linkedin", ""),
                    description=body.get("description", ""),
                    industry=body.get("industry", ""),
                    stage=body.get("stage", ""),
                    location=body.get("location", ""),
                    follow=bool(body.get("follow", True)),
                )
                self._send(200, {"ok": True, "company": rec})

            elif self.path == "/api/undo":
                import io
                from contextlib import redirect_stderr, redirect_stdout

                from .cli import main as cli_main

                buf = io.StringIO()
                with redirect_stdout(buf), redirect_stderr(buf):
                    rc = cli_main(["undo", "--company", body.get("company", "")])
                if rc == 0:
                    self._send(200, {"ok": True, "msg": buf.getvalue().strip().splitlines()[0]})
                else:
                    self._send(400, {"error": buf.getvalue().strip() or "undo failed"})

            elif self.path == "/api/feedback":
                # "Why this score" feedback is a labeled example, same as a
                # mark note — it goes into calibration where the scorer reads
                # it, and into history so insights can count it later.
                judgment = body.get("judgment", "")
                labels = {
                    "too_high": "Overrated by scorer",
                    "too_low": "Underrated by scorer",
                    "right": "Score confirmed",
                }
                if judgment not in labels:
                    self._send(400, {"error": "judgment must be too_high|too_low|right"})
                    return
                entries = feed.load()
                hits = st.find(entries, body.get("url", ""))
                if not hits:
                    self._send(400, {"error": "no entry matched that url"})
                    return
                e = hits[0]
                note = body.get("note", "").strip()
                from . import importer

                cal = importer.load_calibration()
                cal.append(
                    {
                        "title": e.title,
                        "company": e.company,
                        "verdict": labels[judgment],
                        "why": note or f"scored {e.score}; Eric says {labels[judgment].lower()}",
                        "stage_note": e.stage or e.funding,
                        "pay": "",
                    }
                )
                importer.save_calibration(cal)
                hist.record(e.url, "feedback", f"score feedback: {labels[judgment]}", note)
                self._send(200, {"ok": True})

            elif self.path == "/api/profile-capture":
                # The Chrome sitting reads a LinkedIn profile and POSTs the
                # readable text straight here. It exists because the alternative
                # is relaying 8KB of profile through the model's context per
                # person just to get it into a file.
                #
                # Only ever accepts a linkedin.com/in/ URL: the server is
                # 127.0.0.1-only, but any page Eric has open could POST to it,
                # and the guard keeps a stray request from writing junk into
                # the cache under a key the sweep would later trust.
                url = (body.get("linkedin") or "").strip()
                text = body.get("text") or ""
                if "linkedin.com/in/" not in url:
                    self._send(400, {"error": "a linkedin.com/in/ URL is required"})
                    return
                from . import profiles

                ok = profiles.record(url, text)
                self._send(200 if ok else 422,
                           {"ok": ok, "chars": len(text),
                            "error": "" if ok else "too little text — profile did not render"})

            elif self.path == "/api/queue-founder":
                company = body.get("company", "").strip()
                if not company:
                    self._send(400, {"error": "company required"})
                    return
                from .enrich import QUEUE

                q = (
                    json.loads(QUEUE.read_text())
                    if QUEUE.exists()
                    else {"instructions": "Find founders for these companies. Leave a "
                          "field empty rather than guessing. Write back with "
                          "`make apply FILE=...`.",
                          "count": 0, "companies": []}
                )
                if any(c.get("company", "").lower() == company.lower()
                       for c in q["companies"]):
                    self._send(200, {"ok": True, "msg": f"{company} already queued"})
                    return
                entries = feed.load()
                match = next(
                    (x for x in entries if x.company.lower() == company.lower()), None
                )
                q["companies"].append(
                    {"company": company,
                     "title": match.title if match else "",
                     "url": match.url if match else "",
                     "founders": []}
                )
                q["count"] = len(q["companies"])
                QUEUE.write_text(json.dumps(q, indent=1))
                self._send(200, {"ok": True,
                                 "msg": f"{company} queued — next enrichment pass will look up founders"})

            elif self.path == "/api/company-lookup":
                # Autofill from what the pipeline already knows — never invented.
                from .models import normalize_company

                want = normalize_company(body.get("name", ""))
                hit = next((c for c in entities.companies(feed.load())
                            if c.key == want), None)
                if hit:
                    self._send(200, {"ok": True, "found": True, "site": hit.site,
                                     "linkedin": hit.linkedin, "description": hit.blurb,
                                     "industry": hit.industry})
                else:
                    self._send(200, {"ok": True, "found": False})

            elif self.path == "/api/company-mode":
                try:
                    rec = entities.set_company_mode(
                        body.get("company", ""), body.get("mode", ""))
                except ValueError as exc:
                    self._send(400, {"error": str(exc)})
                    return
                dropped = 0
                if body.get("mode") == "uninterested":
                    # "listed as interested" = saved. Live application threads
                    # (applied/interviewing/offer) are real-world state and are
                    # never auto-killed — close those by hand.
                    from .models import normalize_company as _nc
                    entries = feed.load()
                    h = hist.load()
                    key = _nc(body.get("company", ""))
                    for e in entries:
                        if _nc(e.company) == key and e.status == "saved":
                            e.status = "uninterested"
                            e.last_touched = today()
                            h.add(e.url, "status", "moved to uninterested",
                                  "auto — company marked uninterested", at="")
                            dropped += 1
                    if dropped:
                        feed.save(entries)
                        hist.save(h)
                self._send(200, {"ok": True, "company": rec, "dropped": dropped})

            elif self.path == "/api/company-info":
                # Best-effort autofill from the company's OWN site: title/meta
                # description and any linkedin.com/company link on the page.
                # Nothing invented — what isn't there stays blank.
                url = (body.get("url") or "").strip()
                if not url.startswith("http"):
                    url = "https://" + url if url else ""
                if not url:
                    self._send(400, {"error": "give me the company site URL"})
                    return
                import html as _h
                import re as _re

                out = {"site": url, "description": "", "linkedin": ""}
                try:
                    from .boards.base import client
                    with client() as c:
                        r = c.get(url, headers={"Accept": "text/html"})
                        r.raise_for_status()
                        txt = r.text
                    m = (_re.search(r'<meta[^>]+(?:name|property)=["\'](?:og:)?description["\'][^>]*content=["\']([^"\']{20,400})', txt, _re.I)
                         or _re.search(r'content=["\']([^"\']{20,400})["\'][^>]*(?:name|property)=["\'](?:og:)?description', txt, _re.I))
                    if m:
                        out["description"] = _h.unescape(m.group(1)).strip()
                    m = _re.search(r'https?://(?:www\.)?linkedin\.com/company/[A-Za-z0-9._-]+', txt)
                    if m:
                        out["linkedin"] = m.group(0)
                except Exception as exc:  # noqa: BLE001
                    self._send(502, {"error": f"couldn't read the site: {exc}"})
                    return
                self._send(200, out)

            elif self.path == "/api/posting-edit":
                # Hand edits on a posting itself: location, or a job-type
                # override (the bucket is otherwise derived from the title).
                entries = feed.load()
                probe = st.find(entries, body.get("url", ""))
                if not probe:
                    self._send(400, {"error": "no such posting"})
                    return
                for e in probe:
                    if "location" in body:
                        e.location = body["location"]
                    if "role_type" in body:
                        e.role_type = body["role_type"]
                feed.save(entries)
                self._send(200, {"ok": True})

            elif self.path == "/api/interview-stage":
                entries = feed.load()
                probe = st.find(entries, body.get("url", ""))
                if not probe:
                    self._send(400, {"error": "no such posting"})
                    return
                for e in probe:
                    e.interview_stage = body.get("stage", "")
                feed.save(entries)
                h = hist.load()
                if body.get("stage"):
                    h.add(probe[0].url, "note",
                          f"interview stage: {body['stage']}", "", at="")
                    hist.save(h)
                self._send(200, {"ok": True})

            elif self.path == "/api/job-autofill":
                # Re-pull a posting's page and refresh the cached description —
                # for stale, wrong, or missing role text. Cache is auto-data,
                # so overwriting is safe; the feed itself is untouched.
                import html as _h
                import re as _re

                from . import store
                from .models import normalize_url

                url = (body.get("url") or "").strip()
                if not url.startswith("http"):
                    self._send(400, {"error": "no fetchable URL on this posting"})
                    return
                try:
                    from .boards.base import client
                    with client() as c:
                        r = c.get(url, headers={"Accept": "text/html"})
                        r.raise_for_status()
                    txt = _re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", r.text)
                    txt = _h.unescape(_re.sub(r"<[^>]+>", " ", txt))
                    txt = _re.sub(r"\s+", " ", txt).strip()
                except Exception as exc:  # noqa: BLE001
                    self._send(502, {"error": f"couldn't read the posting: {exc}"})
                    return
                if len(txt) < 200:
                    # Inertia apps (Work at a Startup) ship the content as JSON
                    # in data-page — dig out the longest string, which is the
                    # description on every job page checked.
                    m = _re.search(r'data-page="([^"]+)"', r.text)
                    if m:
                        try:
                            def _longest(o):
                                if isinstance(o, str):
                                    return o
                                vals = (o.values() if isinstance(o, dict)
                                        else o if isinstance(o, list) else [])
                                return max((_longest(v) for v in vals),
                                           key=len, default="")
                            blob = _longest(json.loads(_h.unescape(m.group(1))))
                            blob = _h.unescape(_re.sub(r"<[^>]+>", " ", blob))
                            txt = _re.sub(r"\s+", " ", blob).strip()
                        except (ValueError, TypeError):
                            pass
                if len(txt) < 200:
                    self._send(200, {"ok": False,
                                     "error": "page readable but no real text — likely a JS-only page or a wall"})
                    return
                cache = store.load()
                cache[normalize_url(url)] = txt[:8000]
                store.save(cache)
                h = hist.load()
                h.add(url, "note", "job autofill — description re-pulled", "", at="")
                hist.save(h)
                ent = next((e for e in feed.load() if e.url == url), None)
                _log_autofill("Job description", ent.company if ent else url[:40])
                self._send(200, {"ok": True, "chars": len(txt[:8000])})

            elif self.path == "/api/queue-board-sweep":
                # Flags a board for the next Chrome sitting (Wellfound is the
                # only board left that needs a real browser). The sitting
                # protocol reads and clears this file.
                import json as _json

                from .models import today as _today
                board = (body.get("board") or "wellfound").lower()
                qp = _Path(__file__).resolve().parent.parent / "data" / "board_sweep_queue.json"
                try:
                    q = _json.loads(qp.read_text()) if qp.exists() else []
                except ValueError:
                    q = []
                if not any(x.get("board") == board for x in q):
                    q.append({"board": board, "queued_at": _today()})
                    qp.write_text(_json.dumps(q, indent=2))
                self._send(200, {"ok": True, "queued": [x["board"] for x in q]})

            elif self.path == "/api/company-auto":
                # Fill every reachable gap for one company: site → description
                # + LinkedIn, founder queue when nobody's on file, people-sweep
                # queue. Reports what it did; invents nothing.
                from .models import normalize_company as _nc

                name = body.get("company", "")
                lane = body.get("lane", "both")     # company | people | both
                rec = entities.load_company_overlay().get(_nc(name), {})
                did = []
                # overlay site first, then the client's derived one (postings)
                site = ""
                if lane in ("company", "both"):
                    site = rec.get("site", "") or (body.get("site") or "").strip()
                if not site and lane in ("company", "both"):
                    # No site on record: find it. DuckDuckGo's HTML endpoint,
                    # first result whose domain echoes the company name —
                    # anything less certain stays blank rather than guessed.
                    site = _discover_site(name)
                    if site:
                        entities.track_company(name, site=site, follow=False)
                        rec = entities.load_company_overlay().get(_nc(name), {})
                        did.append(f"website found: {site.split('//')[-1]}")
                if site and (not rec.get("description") or not rec.get("linkedin")
                             or not rec.get("industry")):
                    try:
                        import html as _h
                        import re as _re

                        from .boards.base import client
                        with client() as c:
                            r = c.get(site if site.startswith("http") else "https://" + site)
                            r.raise_for_status()
                        desc = ""
                        m = _re.search(r'<meta[^>]+(?:name|property)=["\'](?:og:)?description["\'][^>]*content=["\']([^"\']{20,400})', r.text, _re.I)
                        if m:
                            desc = _h.unescape(m.group(1)).strip()
                        if not desc:
                            for pm in _re.finditer(r"<p[^>]*>(.*?)</p>", r.text, _re.S | _re.I):
                                cand = _re.sub(r"<[^>]+>", " ", pm.group(1))
                                cand = _h.unescape(_re.sub(r"\s+", " ", cand)).strip()
                                if 60 <= len(cand) <= 400:
                                    desc = cand
                                    break
                        li = ""
                        m = _re.search(r'https?://(?:www\.)?linkedin\.com/company/[A-Za-z0-9._-]+', r.text)
                        if m:
                            li = m.group(0)
                        if (desc and not rec.get("description")) or (li and not rec.get("linkedin")):
                            entities.track_company(
                                name,
                                description=desc if not rec.get("description") else "",
                                linkedin=li if not rec.get("linkedin") else "",
                                follow=False)
                            if desc and not rec.get("description"):
                                did.append("description from site")
                            if li and not rec.get("linkedin"):
                                did.append("LinkedIn found")
                        if not rec.get("industry"):
                            ind = _guess_industry(desc + " " + r.text[:4000])
                            if ind:
                                entities.track_company(name, industry=ind, follow=False)
                                did.append(f"industry guessed: {ind} (edit if wrong)")
                    except Exception:  # noqa: BLE001 — a dead site just skips
                        did.append("site unreachable")
                elif not site and lane in ("company", "both"):
                    did.append("no website on file and none found — add one manually")
                rec = entities.load_company_overlay().get(_nc(name), {})
                if lane in ("company", "both") and not rec.get("industry") and rec.get("description"):
                    ind = _guess_industry(rec["description"])
                    if ind:
                        entities.track_company(name, industry=ind, follow=False)
                        did.append(f"industry guessed: {ind} (edit if wrong)")
                if lane in ("company", "both") and not rec.get("stage"):
                    stg, amt = _formd_stage(name)
                    if stg:
                        entities.track_company(name, stage=stg, follow=False)
                        did.append(f"stage from Form D: {stg.replace('_', ' ')}"
                                   + (f" (${amt:.1f}M raised)" if amt else ""))
                if any(("found" in x or "guessed" in x or "description" in x
                        or "stage" in x) for x in did):
                    _log_autofill("Company details", name)
                from .enrich import QUEUE as _EQ
                entries = feed.load()
                has_people = any(
                    _nc(e.company) == _nc(name) and e.founders for e in entries)
                if lane == "people":
                    has_people = True          # people lane never queues founders
                if not has_people:
                    try:
                        raw = json.loads(_EQ.read_text())
                    except (OSError, ValueError):
                        raw = {}
                    # the file is enrich.write_queue's wrapper: keep its shape
                    if isinstance(raw, list):
                        raw = {"companies": raw}
                    comps_q = raw.setdefault("companies", [])
                    if not any(isinstance(x, dict) and _nc(x.get("company", "")) == _nc(name)
                               for x in comps_q):
                        comps_q.append({"company": name, "queued": today()})
                        raw["count"] = len(comps_q)
                        _EQ.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n")
                    did.append("founder lookup queued (server)")
                if lane in ("people", "both") and entities.queue_alumni(name):
                    did.append("founders autofill queued (Chrome)")
                self._send(200, {"ok": True, "did": did})

            elif self.path == "/api/queue-alumni":
                added = entities.queue_alumni(body.get("company", ""))
                self._send(200, {"ok": True, "queued": added})

            elif self.path == "/api/company-interest":
                rec = entities.set_company_interest(
                    body.get("company", ""), bool(body.get("interested", True))
                )
                self._send(200, {"ok": True, "company": rec})

            elif self.path == "/api/person-follow":
                rec = entities.set_person_follow(
                    body.get("name", ""), body.get("company", ""),
                    bool(body.get("following")))
                self._send(200, {"ok": True, "person": rec})

            elif self.path == "/api/person-fill-queue":
                # Just a LinkedIn URL is enough: the person lands name-guessed
                # and queued; a Chrome sitting visits the profile and fills
                # role/company/education/location for real. Profiles are
                # login-walled, so the server never tries.
                url = (body.get("linkedin") or "").strip()
                if "linkedin.com/in/" not in url:
                    self._send(400, {"error": "paste a linkedin.com/in/… profile URL"})
                    return
                fq = dashboard.ROOT / "data" / "person_fill_queue.json"
                try:
                    q = json.loads(fq.read_text())
                except (OSError, ValueError):
                    q = []
                if not any(x.get("linkedin", "").rstrip("/") == url.rstrip("/") for x in q):
                    q.append({"linkedin": url.rstrip("/"),
                              "name": body.get("name", ""), "queued": today()})
                    fq.write_text(json.dumps(q, indent=2, ensure_ascii=False) + "\n")
                self._send(200, {"ok": True, "queued": len(q)})

            elif self.path == "/api/tweet-status":
                # The X ledger is xsearch.py's file; the app only ever flips
                # one tweet's status in place, keyed by its URL, so a sweep
                # running alongside never loses text it just wrote.
                url = (body.get("url") or "").strip().rstrip("/")
                status = (body.get("status") or "").strip()
                if status not in ("new", "saved", "dismissed"):
                    self._send(400, {"error": "status: new, saved or dismissed"})
                    return
                fp = dashboard.ROOT / "data" / "x_tweets.json"
                try:
                    ledger = json.loads(fp.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    ledger = {"tweets": []}
                hit = next((x for x in ledger.get("tweets", [])
                            if (x.get("url") or "").rstrip("/") == url), None)
                if hit is None:
                    self._send(404, {"error": "tweet not in the ledger"})
                    return
                hit["status"] = status
                fp.write_text(json.dumps(ledger, indent=1, ensure_ascii=False))
                self._send(200, {"ok": True, "tweet": hit})

            elif self.path == "/api/nudge":
                entities.record_nudge(body.get("name", ""), body.get("company", ""),
                                      body.get("date", ""))
                self._send(200, {"ok": True})

            elif self.path == "/api/person-status":
                try:
                    rec = entities.set_person_status(
                        body.get("name", ""), body.get("company", ""),
                        body.get("status", ""), when=body.get("date", ""),
                        create=bool(body.get("create")),
                        method=body.get("method", ""), detail=body.get("detail", ""),
                    )
                    # Saving (or advancing) someone unenriched queues their
                    # profile fill — review/uninterested moves never do.
                    fill_queued = False
                    if body.get("status", "") not in ("review", "uninterested"):
                        fill_queued = entities.queue_person_fill(rec)
                    self._send(200, {"ok": True, "person": rec,
                                     "fill_queued": fill_queued})
                except (ValueError, KeyError) as exc:
                    self._send(400, {"error": str(exc)})

            elif self.path == "/api/person":
                warm_raw = body.get("warm", None)
                rec = entities.add_person(
                    name=body.get("name", ""),
                    warm=(None if warm_raw is None else bool(warm_raw)),
                    warm_via=body.get("warm_via", ""),
                    company=body.get("company", ""),
                    role=body.get("role", ""),
                    linkedin=body.get("linkedin", ""),
                    relationship=body.get("relationship", "networking"),
                    signal=body.get("signal", ""),
                    note=body.get("note", ""),
                    # The dashboard's edit form sends these; dropping them here
                    # was why a phone number typed into the modal never saved.
                    email=body.get("email", ""),
                    x=body.get("x", ""),
                    phone=body.get("phone", ""),
                    location=body.get("location", ""),
                    notes_replace=body.get("notes", None),
                )
                # A manual add of someone unenriched queues their fill too.
                fill_queued = entities.queue_person_fill(rec)
                self._send(200, {"ok": True, "person": rec,
                                 "fill_queued": fill_queued})

            else:
                self._send(404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001 — surface, don't crash the server
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})


def _lan_ip() -> str:
    """Best-guess address this machine is reachable at, for the printed URL."""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 53))      # no packets sent; just picks the route
        return s.getsockname()[0]
    except OSError:
        return HOST
    finally:
        s.close()


def serve(open_browser: bool = True) -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    loopback = HOST in ("127.0.0.1", "localhost", "::1")
    url = f"http://{'127.0.0.1' if loopback else _lan_ip()}:{PORT}/"
    if loopback:
        print(f"Job Pipeline app: {url}   (Ctrl-C to stop — local only, nothing exposed)")
    else:
        print(f"Job Pipeline app: {url}   (Ctrl-C to stop)")
        print(f"  ⚠ bound to {HOST} — REACHABLE BY ANYTHING ON THIS NETWORK.")
        print("  ⚠ There is no login, and the API can change your pipeline data.")
        print("  ⚠ Trusted wifi only. Unset JOBS_HOST to go back to loopback.")
    if open_browser and loopback:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
