"""Entrypoints. `make scout` lands here."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from . import feed, prefilter, report, score
from .boards import run_board
from .models import STATUSES, Entry as Entry_t
from .models import RawPosting, normalize_company, normalize_url, today

ROOT = Path(__file__).resolve().parent.parent
BOARDS_PATH = ROOT / "data" / "boards.yaml"


def _load_boards(only: list[str] | None) -> dict[str, dict]:
    cfg = yaml.safe_load(BOARDS_PATH.read_text(encoding="utf-8"))["boards"]
    if not only:
        return cfg
    missing = [b for b in only if b not in cfg]
    if missing:
        sys.exit(f"unknown board(s): {', '.join(missing)}\nknown: {', '.join(cfg)}")
    return {b: cfg[b] for b in only}


def scout(args: argparse.Namespace) -> int:
    load_dotenv(ROOT / ".env")
    boards = _load_boards(args.board)

    results, all_postings, kept_by_board = [], [], {}
    dropped_total: dict[str, int] = {}

    for name, board_cfg in boards.items():
        print(f"  fetching {name}…", file=sys.stderr)
        r = run_board(name, board_cfg)
        results.append(r)
        kept, dropped = prefilter.run(r.postings)
        kept_by_board[name] = len(kept)
        for reason, n in dropped.items():
            dropped_total[reason] = dropped_total.get(reason, 0) + n
        all_postings.extend(kept)

    from . import store

    cached = store.remember(all_postings)
    if cached:
        print(f"  cached {cached} new job descriptions", file=sys.stderr)

    # Companies discovered without a posting (EDGAR fresh raises) go to the
    # company overlay, not the feed — the play retirement, 2026-08-12. Dry
    # runs report but write nothing.
    discovered = [cd for r in results for cd in r.companies]
    if discovered:
        if args.dry_run:
            print(f"  {len(discovered)} fresh-raise companies found (dry run — not written)",
                  file=sys.stderr)
        else:
            from . import entities as _entco
            disc_n = sum(1 for cd in discovered if _entco.discover_company(cd))
            print(f"  {disc_n} fresh-raise companies entered company review "
                  f"({len(discovered) - disc_n} already known)", file=sys.stderr)

    existing = feed.load()
    fresh, dupes = feed.merge_new(existing, all_postings)
    print(f"  {len(fresh)} new after dedupe ({dupes} already known)", file=sys.stderr)

    if args.limit:
        fresh = fresh[: args.limit]

    # Nothing enters the pile unjudged (Eric, 2026-08-18). A posting that
    # arrived with no text gets one free fetch attempt right here — the same
    # ATS-JSON and HTML routes the weekly sweep uses — so it is usually
    # scorable by the time it lands rather than days later.
    if not args.dry_run:
        blind_now = [p for p in fresh if not (p.description or "").strip()]
        if blind_now:
            try:
                from . import refetch as _rf0

                got = _rf0.fill_postings(blind_now)
                if got:
                    # Cache what the fill just fetched. store.remember() ran
                    # BEFORE this, over the postings as the boards shipped
                    # them, so text fetched here was scored once and then
                    # thrown away (found 2026-08-30). With the API parked the
                    # scorer returns None, so those rows landed in `review`
                    # unscored AND textless — permanently unscorable for any
                    # host the sweep can't reach, which is how 186 LinkedIn
                    # rows got stuck there.
                    store.remember(blind_now)
                    print(f"  filled {got}/{len(blind_now)} descriptions inline "
                          "before scoring", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001 — never sink a good scout
                print(f"  (inline fill skipped: {type(exc).__name__})", file=sys.stderr)

    judgeable = [p for p in fresh if (p.description or "").strip()]
    blind = [p for p in fresh if not (p.description or "").strip()]
    scored, backend = score.run(judgeable)
    print(f"  scored via '{backend}' backend"
          + (f"  ({len(blind)} held as incomplete — no text to judge)" if blind else ""),
          file=sys.stderr)

    # Whatever still has no text is held OUT of the triage pile as
    # `incomplete`: it stays in the ledger (which is also the dedupe memory,
    # so a holding file would drift) and promotes itself to `review` the day
    # text and a score arrive.
    def _held(p):
        e = feed.to_entry(p, None, "", False)
        e.status = "incomplete"
        e.status_note = "no description available yet — held out of review until scored"
        return e

    new_entries = [
        feed.to_entry(s.posting, s.score, s.why, s.escape_hatch, s.level)
        for s in scored
    ] + [_held(p) for p in blind]

    if args.dry_run:
        print("\n(dry run — feed.json not written)", file=sys.stderr)
    else:
        merged = existing + new_entries
        from .dashboard import apply_sweep, promote_complete

        promo_n = promote_complete(merged)
        if promo_n:
            print(f"  promoted {promo_n} postings out of incomplete "
                  "(text + score arrived)", file=sys.stderr)
        swept_n = apply_sweep(merged)
        if swept_n:
            print(f"  swept {swept_n} stale review postings to uninterested", file=sys.stderr)
        # Completion pass: new postings should arrive as complete as possible.
        # Free founder propagation across same-company entries, then a bounded
        # sweep of company sites for meta descriptions + LinkedIn links.
        from .enrich import complete_companies, propagate_known

        prop_n = propagate_known(merged)
        desc_n, li_n = complete_companies(merged, cap=25)
        if prop_n or desc_n or li_n:
            print(f"  completed: {prop_n} founder propagations, "
                  f"{desc_n} descriptions, {li_n} LinkedIn links from company sites",
                  file=sys.stderr)
        # Second step after import: every company that arrived with a LinkedIn
        # joins the Chrome-sitting queue. The sitting drains 10 per run, five
        # runs a day — the queue is the throttle, so queuing broadly is safe.
        from . import entities as _ent
        from .models import normalize_company as _nc

        new_cos = {_nc(e.company) for e in new_entries if e.company}
        overlay = _ent.load_company_overlay()
        q_n = sum(1 for k in new_cos
                  if overlay.get(k, {}).get("linkedin") and _ent.queue_alumni(overlay[k].get("name", k)))
        if q_n:
            print(f"  queued {q_n} imported companies for the LinkedIn sitting", file=sys.stderr)
        feed.save(merged)
        _refresh_dash()
        # Substack seen-state commits only after the feed is safely written —
        # same ordering rule as funding.save_state below, same reason.
        from .boards import substack as _substack

        _substack.commit_seen()
        # Newsletter prose leads extract themselves when an API key is
        # present (bounded per run), exactly like scoring — the queue file
        # is the no-key fallback, not the default experience.
        try:
            from . import newsletter as _nl

            nl = _nl.auto_run()
            if nl:
                print(f"  newsletter extraction: {nl['companies']} companies, "
                      f"{nl['people']} people to review, {nl['postings']} postings "
                      f"({nl['remaining']} leads still queued)", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 — never sink a good scout
            print(f"  (newsletter extraction skipped: {type(exc).__name__})",
                  file=sys.stderr)
        # Companies that arrived with no self-description anywhere get
        # researched (web search, identity-guarded), bounded per run.
        try:
            from . import research as _rs

            rs = _rs.auto_fill(feed.load())
            if rs:
                print(f"  company research: {rs['identified']}/{rs['researched']} "
                      f"identified — {rs['description']} descriptions, "
                      f"{rs['industry']} industries, {rs['site']} sites",
                      file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"  (company research skipped: {type(exc).__name__})",
                  file=sys.stderr)
        # Link sweep: watched postings (saved/applied/interviewing) get their
        # pages checked every run; the wider descriptionless pile drains
        # weekly. Plain HTTP — no tokens (Eric, 2026-08-17).
        try:
            from . import refetch as _rf

            _sw_entries = feed.load()
            sw = _rf.ride_scout(_sw_entries)
            if sw and (sw["expired"] or sw["described"]):
                feed.save(_sw_entries)
                _refresh_dash()
        except Exception as exc:  # noqa: BLE001
            print(f"  (link sweep skipped: {type(exc).__name__})", file=sys.stderr)
        # Saved companies with no founder get one researched, which is also
        # what makes their profiles readable — every connection signal is
        # downstream of having a person to read.
        try:
            from . import research as _rs2

            # One list, mutated then saved. Loading a second copy to save
            # would have thrown away every founder just written.
            _ents = feed.load()
            fr = _rs2.auto_fill_founders(_ents)
            if fr:
                print(f"  founder research: {fr['identified']}/{fr['companies']} "
                      f"companies -> {fr['founders']} founders, "
                      f"{fr['queued_fills']} profile fills queued", file=sys.stderr)
                if fr["founders"]:
                    feed.save(_ents)
                    _refresh_dash()
        except Exception as exc:  # noqa: BLE001
            print(f"  (founder research skipped: {type(exc).__name__})", file=sys.stderr)
        # X hiring-tweet sweep via Grok — one capped call a day, only when
        # XAI_API_KEY is set. A $2/mo experiment; make insights is the judge.
        try:
            from . import xsearch as _xs

            xs = _xs.sweep()
            if xs:
                print(f"  X sweep: {xs['tweets']} fresh tweets to the ledger "
                      f"({xs['postings']} carried application links -> postings)",
                      file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"  (X sweep skipped: {type(exc).__name__})", file=sys.stderr)

    print(
        report.render(results, kept_by_board, dropped_total, dupes, new_entries, backend)
    )

    # A round closing is the single best outreach trigger there is, and it is
    # useless if nobody notices for three weeks. The daily scout is already
    # scheduled, so the watch rides along with it — one SEC request per watched
    # company, skipped entirely on a dry run or when explicitly disabled.
    if not args.dry_run and not args.no_funding:
        try:
            from . import funding

            entries = feed.load()
            names = funding.watch_list(entries)
            print(f"checking {len(names)} watched companies for new raises…",
                  file=sys.stderr)
            raises, state = funding.check(names)
            # Save state LAST. It records which filings we've already reported,
            # so writing it before the feed means a failure here marks the round
            # seen while never recording it — and the except below swallows the
            # error, so the raise would vanish silently and permanently.
            if raises:
                funding.apply(entries, raises)
                feed.save(entries)
                _refresh_dash()
                print(funding.render(raises, len(names)))
            stamped = funding.stamp_overlay(state)
            if stamped:
                print(f"  raise dates: {stamped} companies stamped with latest Form D",
                      file=sys.stderr)
            funding.save_state(state)
        except Exception as exc:  # noqa: BLE001 — never sink a good scout
            print(f"\n(funding watch skipped: {type(exc).__name__})", file=sys.stderr)
    return 0


def import_tracker(args: argparse.Namespace) -> int:
    from . import importer

    path = Path(args.file).expanduser()
    if not path.exists():
        sys.exit(f"no such file: {path}")

    rows = importer.read_rows(path)
    stats = importer.ImportStats(rows=len(rows))

    existing = feed.load()
    index = feed.Index(existing)
    new_entries: list[Entry_t] = []
    seen_this_run: set[str] = set()

    for row in rows:
        e = importer.to_entry(row, source=args.source)
        if e is None:
            stats.skipped_bucket += 1
            continue
        probe = RawPosting(title=e.title, company=e.company, url=e.url, source=e.source)
        if index.find(probe) or e.identity in seen_this_run:
            stats.skipped_dupe += 1
            continue
        seen_this_run.add(e.identity)
        new_entries.append(e)
        stats.added += 1

    # Calibration takes every row, screened-out included — the rejections are
    # the most informative examples the scorer can have.
    cal = importer.load_calibration()
    known = {(c["title"], c["company"]) for c in cal}
    for row in rows:
        rec = importer.to_calibration(row)
        if rec and (rec["title"], rec["company"]) not in known:
            cal.append(rec)
            known.add((rec["title"], rec["company"]))
            stats.calibration += 1

    if args.dry_run:
        print("(dry run — nothing written)")
    else:
        feed.save(existing + new_entries)
        importer.save_calibration(cal)

    print(f"\nRows read:            {stats.rows}")
    print(f"Added to feed:        {stats.added}")
    print(f"Skipped (dupe):       {stats.skipped_dupe}")
    print(f"Skipped (not a job):  {stats.skipped_bucket}")
    print(f"Calibration examples: +{stats.calibration} (total {len(cal)})")

    if new_entries:
        print("\nShortlisted:")
        for e in new_entries:
            if e.status == "saved":
                print(f"  {e.score}  {e.title[:44]:44} | {e.company[:20]:20} | {e.location[:18]}")
    return 0


def apply_cmd(args: argparse.Namespace) -> int:
    """One door for every in-session result file.

    There used to be four near-identical commands — apply-scores,
    apply-enrichment, apply-descriptions, apply-resolution — and remembering
    which one a given file belonged to was pure friction, since the queue that
    produced it already knew. The shapes are distinguishable, so this sniffs
    and dispatches. `--as` forces it when a file is ambiguous or empty.
    """
    import json as _json

    payload = _json.loads(Path(args.file).expanduser().read_text(encoding="utf-8"))

    kind = args.as_kind or _sniff_result(payload)
    if not kind:
        sys.exit(
            "couldn't tell what this file is. Pass --as "
            "scores|enrichment|descriptions|resolution"
        )
    print(f"detected: {kind}")
    return {
        "scores": apply_scores,
        "enrichment": apply_enrichment_cmd,
        "descriptions": apply_descriptions_cmd,
        "resolution": apply_resolution_cmd,
        "substack": apply_substack_cmd,
    }[kind](args)


def _sniff_result(payload) -> str:
    """Identify a result file by shape. Returns "" when genuinely ambiguous."""
    if isinstance(payload, list):
        first = payload[0] if payload else {}
        if isinstance(first, dict):
            if "founders" in first or "funding" in first:
                return "enrichment"
            if "linkedin" in first or "profile" in first:
                return "resolution"
        return ""
    if not isinstance(payload, dict):
        return ""
    # Nested {companies: [...]} / {people: [...]} results echo their queue.
    for key, kind in (("companies", "enrichment"), ("people", "resolution")):
        if isinstance(payload.get(key), list):
            return kind
    vals = [v for v in payload.values() if v is not None]
    if vals and all(isinstance(v, str) for v in vals):
        return "descriptions"
    if vals and all(isinstance(v, dict) for v in vals):
        keys = set().union(*(v.keys() for v in vals if isinstance(v, dict)))
        if "kind" in keys:
            return "substack"
        if {"score", "why"} & keys:
            return "scores"
        if {"linkedin", "profile"} & keys:
            return "resolution"
        if {"founders", "funding", "headcount"} & keys:
            return "enrichment"
    return ""


def apply_substack_cmd(args: argparse.Namespace) -> int:
    """Route in-session substack extraction results (data/substack_queue.json).

    Result file: {item_index: {kind, name, company, role, url, contact_hint,
    why, quote}}. Companies land in the overlay automatically; people go to
    the review queue, never straight into People (honesty rule); postings are
    appended unscored (make rescore picks them up via the cached description).
    """
    import json as _json

    from . import newsletter
    from .boards import substack as _ss

    payload = _json.loads(Path(args.file).expanduser().read_text(encoding="utf-8"))
    try:
        queue = _json.loads(_ss.QUEUE.read_text()).get("items", [])
    except (FileNotFoundError, _json.JSONDecodeError):
        queue = []

    entries = feed.load()
    counts = newsletter.route_results(payload, queue, entries)
    handled = counts.pop("handled")
    if counts["postings"] and not args.dry_run:
        feed.save(entries)
        _refresh_dash()
    if not args.dry_run:
        newsletter.rewrite_queue(queue, handled)
    print(f"substack results: {counts['companies']} companies tracked, "
          f"{counts['people']} people queued for review, {counts['postings']} "
          f"postings added (unscored — run make rescore)")
    return 0


def substack_backfill_cmd(args: argparse.Namespace) -> int:
    """One-time 3-month seed of the substack source via the archive API."""
    from . import prefilter, store
    from .boards import substack as _ss

    cfg = _load_boards(["substack"]).get("substack")
    if not cfg:
        sys.exit("no 'substack' block in data/boards.yaml")
    raw, done, paid = _ss.backfill("substack", cfg, months=args.months)
    kept, dropped = prefilter.run(raw)
    store.remember(kept)
    entries = feed.load()
    fresh, dupes = feed.merge_new(entries, kept)
    entries += [feed.to_entry(p, None, "", False) for p in fresh]
    if args.dry_run:
        print("(dry run — nothing written)")
        return 0
    # Same completion pass the scout runs — a backfill that skips it leaves
    # hundreds of companies with no site/description (learned live 2026-08-06).
    from .enrich import complete_companies, propagate_known

    prop_n = propagate_known(entries)
    desc_n, li_n = complete_companies(entries, cap=100)
    if prop_n or desc_n or li_n:
        print(f"  completed: {prop_n} founder propagations, {desc_n} descriptions, "
              f"{li_n} LinkedIn links from company sites")
    feed.save(entries)
    _refresh_dash()
    _ss.commit_seen()
    try:
        from . import newsletter as _nl

        nl = _nl.auto_run(limit=None)  # one-time seed: drain the whole queue
        if nl:
            print(f"newsletter extraction: {nl['companies']} companies, "
                  f"{nl['people']} people to review, {nl['postings']} postings "
                  f"({nl['remaining']} leads left)")
    except Exception as exc:  # noqa: BLE001
        print(f"(newsletter extraction skipped: {type(exc).__name__} — queue kept)")
    try:
        from . import research as _rs

        rs = _rs.auto_fill(feed.load(), cap=150)
        if rs:
            print(f"company research: {rs['identified']}/{rs['researched']} identified — "
                  f"{rs['description']} descriptions, {rs['industry']} industries, "
                  f"{rs['site']} sites")
    except Exception as exc:  # noqa: BLE001
        print(f"(company research skipped: {type(exc).__name__})")
    print(f"backfill: {done} posts processed ({paid} paywalled skipped), "
          f"{len(raw)} job links found, {len(kept)} after prefilter "
          f"({sum(dropped.values())} dropped), {len(fresh)} new after dedupe "
          f"({dupes} already known).")
    print("New entries are unscored — run `make rescore` to score them.")
    return 0


def mirror_cmd(args: argparse.Namespace) -> int:
    """Recover walled descriptions via web search (costs credits)."""
    load_dotenv(ROOT / ".env")
    from . import mirror

    mirror.run(feed.load(), cap=args.limit, max_spend=args.max_spend, yes=args.yes)
    return 0


def levels_cmd(args: argparse.Namespace) -> int:
    """Backfill seniority level from cached descriptions (no rescore)."""
    load_dotenv(ROOT / ".env")
    from . import store

    entries = feed.load()
    descriptions = store.load()
    live = ("review", "saved", "applied", "interviewing", "offer")
    targets = [
        e for e in entries
        if e.status in live
        and (args.all or not e.level)
        and len(descriptions.get(normalize_url(e.url), "")) >= 300
    ]
    if args.limit:
        targets = targets[: args.limit]
    if not targets:
        print("nothing to level.")
        return 0
    print(f"{len(targets)} postings to level  ·  ~${len(targets) * 900 / 1e6 / 2:.2f}")
    posts = [
        RawPosting(title=e.title, company=e.company, url=e.url, source=e.source,
                   location=e.location,
                   description=descriptions[normalize_url(e.url)])
        for e in targets
    ]
    got = score.levels_for(posts)
    by_url = {normalize_url(u): v for u, v in got.items()}
    n = 0
    for e in targets:
        lvl = by_url.get(normalize_url(e.url))
        if lvl:
            e.level = lvl
            n += 1
    feed.save(entries)
    from collections import Counter

    dist = Counter(e.level for e in entries if e.level)
    print(f"levelled {n} of {len(targets)} — {dict(dist)}")
    return 0



    """Bulk Repopulate Job + dead-link sweep in one HTTP walk (no tokens)."""
    from . import refetch

    entries = feed.load()
    counts = refetch.sweep(entries, cap=args.limit,
                           check_all_liveness=args.all_liveness)
    if counts["expired"] or counts["described"]:
        feed.save(entries)
    if counts["described"]:
        print(f"{counts['described']} postings now have real text — "
              "run `make rescore` to score them (and their yoe chips fill on reload).")
    return 0


def repopulate_cmd(args: argparse.Namespace) -> int:
    """Bulk Repopulate Job + dead-link sweep in one HTTP walk (no tokens)."""
    from . import refetch

    entries = feed.load()
    counts = refetch.sweep(entries, cap=args.limit,
                           check_all_liveness=args.all_liveness)
    if counts["expired"] or counts["described"]:
        feed.save(entries)
    if counts["described"]:
        print(f"{counts['described']} postings now have real text — "
              "run `make rescore` to score them (and their yoe chips fill on reload).")
    return 0


def apply_scores(args: argparse.Namespace) -> int:
    """Write scores back into the feed — the return half of the agent backend.

    `make scout` with no API key emits data/candidates.json and stops. Claude
    Code scores those in-session and writes a JSON map back through here, so the
    no-key path is a complete loop rather than a dead end. Same file shape the
    api backend produces internally:
    {url_fragment: {score, why, escape_hatch?, level?}}.
    """
    import json as _json

    payload = _json.loads(Path(args.file).expanduser().read_text(encoding="utf-8"))
    entries = feed.load()

    updated, unmatched = 0, []
    for frag, verdict in payload.items():
        hits = [e for e in entries if frag in e.url]
        if not hits:
            unmatched.append(frag)
            continue
        for e in hits:
            e.score = verdict.get("score")
            e.why = verdict.get("why", e.why)
            e.escape_hatch = bool(verdict.get("escape_hatch", False))
            if verdict.get("level"):
                e.level = str(verdict["level"]).strip().lower()
            if verdict.get("status"):
                e.status = verdict["status"]
            e.last_touched = today()
            updated += 1

    if args.dry_run:
        print("(dry run — nothing written)")
    else:
        feed.save(entries)

    if not args.dry_run:
        _refresh_dash()
    print(f"updated {updated} entries from {len(payload)} verdicts")
    if unmatched:
        print(f"no feed entry matched {len(unmatched)}: {', '.join(unmatched[:5])}")
    return 0


def xsweep_cmd(args: argparse.Namespace) -> int:
    """Manual X hiring-tweet sweep — same capped call the scout makes."""
    import os

    load_dotenv(ROOT / ".env")
    from . import xsearch

    if not os.environ.get("XAI_API_KEY"):
        print("no XAI_API_KEY in .env — create one at console.x.ai, then retry")
        return 1
    counts = xsearch.sweep(force=args.force, lens=args.lens)
    if counts is None:
        # sweep prints its own error line when the call failed; this branch
        # is also the once-a-day cap. Say which one actually happened.
        from datetime import date as _d

        if xsearch._load(xsearch.SEEN).get("last_run") == _d.today().isoformat() and not args.force:
            print("already ran today (once-a-day cap) — use FORCE=1 to override")
        else:
            print("sweep did not run — see the error above (fix, then retry; "
                  "nothing was stamped)")
        return 1
    _refresh_dash()
    print(f"{counts['tweets']} fresh tweets in data/x_tweets.json "
          f"({counts['postings']} carried application links -> unscored postings)")
    return 0


def rescore_cmd(args: argparse.Namespace) -> int:
    """Score entries already in the feed.

    `make scout` only scores postings it just fetched, so anything that landed
    with score: null — everything the agent backend passed through — stays
    unscored forever. This works that backlog, pulling descriptions back out of
    the sidecar cache so the scorer sees what it saw the first time.
    """
    load_dotenv(ROOT / ".env")
    from . import store

    entries = feed.load()
    targets = [
        e
        for e in entries
        if (e.score is None or args.all)
        and e.status not in ("uninterested", "applied", "interviewing", "offer", "rejected", "dormant")
    ]
    if args.company:
        from . import status as st

        wanted = {id(x) for x in st.find(entries, args.company)}
        targets = [e for e in targets if id(e) in wanted]
    descriptions = store.load()

    # Score the postings we can actually judge first. Feed order front-loads the
    # Techstars rows, which mostly have no description — scoring those first
    # means a --limit run spends its whole budget on the weakest inputs. Within
    # each group, keep feed order so runs stay reproducible.
    targets.sort(key=lambda e: 0 if normalize_url(e.url) in descriptions else 1)

    if args.limit:
        targets = targets[: args.limit]

    if not targets:
        print("nothing to rescore.")
        return 0
    backend = score.resolve_backend()
    model = __import__("os").environ.get("SCORER_MODEL") or score.DEFAULT_MODEL

    with_desc = sum(1 for e in targets if normalize_url(e.url) in descriptions)
    print(f"{len(targets)} entries to rescore  ({with_desc} with cached descriptions)")
    print(f"backend: {backend}" + (f"  ·  model: {model}" if backend == "api" else ""))

    if backend == "api":
        projected = score.estimate_cost(len(targets), model)
        print(f"projected cost: ${projected:.2f}")
        if projected > args.max_spend and not args.yes:
            print(
                f"\nrefusing to start: ${projected:.2f} exceeds the "
                f"${args.max_spend:.2f} guard.\n"
                "  raise it with --max-spend, cap the batch with --limit, "
                "or pass --yes to override.",
                file=sys.stderr,
            )
            return 1
        if not args.yes:
            print("  (under the guard — proceeding)")

    blind = [e for e in targets if not descriptions.get(normalize_url(e.url), "").strip()]
    if blind:
        print(f"  skipping {len(blind)} entries with no description on file —"
              " Repopulate Job (or a scout refetch) unlocks them")
        targets = [e for e in targets if e not in blind]
        if not targets:
            print("nothing left to rescore.")
            return 0
    posts = [
        RawPosting(
            title=e.title,
            company=e.company,
            url=e.url,
            source=e.source,
            location=e.location,
            stage=e.stage,
            industry_tags=list(e.also_seen_on),
            description=descriptions[normalize_url(e.url)],
        )
        for e in targets
    ]

    scored, backend = score.run(posts)
    by_url = {normalize_url(s.posting.url): s for s in scored}

    updated = 0
    for e in targets:
        s = by_url.get(normalize_url(e.url))
        if s and s.score is not None:
            e.score, e.why, e.escape_hatch = s.score, s.why, s.escape_hatch
            if s.level:
                e.level = s.level
            e.last_touched = today()
            updated += 1

    floored = promoted = 0
    if not args.dry_run:
        from .dashboard import apply_sweep, promote_complete

        # Promote BEFORE the floor so a newly-scored incomplete row is judged
        # by the same rule as everything else: joins review, or floors out.
        promoted = promote_complete(entries)
        floored = apply_sweep(entries)

    if args.dry_run:
        print("\n(dry run — feed not written)")
    else:
        feed.save(entries)

    print(f"\nscored {updated} of {len(targets)}"
          + (f" · {promoted} promoted out of incomplete" if promoted else "")
          + (f" · {floored} fell to the score floor" if floored else ""))
    if updated:
        ranked = sorted(
            (e for e in targets if e.score is not None), key=lambda x: -(x.score or 0)
        )
        print("\ntop results:")
        for e in ranked[:8]:
            print(f"  {e.score:>3}  {e.title[:40]:40} | {e.company[:18]:18}")
    elif backend == "agent":
        print("  candidates written to data/candidates.json for in-session scoring")
    return 0


def log_cmd(args: argparse.Namespace) -> int:
    from . import history as hist
    from . import status as st

    entries = feed.load()
    hits = st.find(entries, args.company)
    if not hits:
        print(f"nothing matched '{args.company}'", file=sys.stderr)
        return 1
    hits.sort(key=lambda e: -(e.score or 0))
    e = hits[0]
    h = hist.load()

    if args.note:
        h.add(e.url, "note", args.note)
        hist.save(h)
        # keep the one-line summary in sync for the status view
        e.status_note = args.note
        e.last_touched = today()
        feed.save(entries)
        print(f"logged: {args.note}")
        _refresh_dash()
        return 0

    print(f"\n{e.title} — {e.company}  [{e.status}]  score {e.score}")
    print(hist.render(e, h))
    print()
    return 0


def today_cmd(args: argparse.Namespace) -> int:
    from . import today as td

    print(td.render(feed.load()))
    return 0


def undo_cmd(args: argparse.Namespace) -> int:
    """Revert an entry's last status change, using history as the source of truth."""
    from . import history as hist
    from . import status as st

    entries = feed.load()
    hits = st.find(entries, args.company)
    if not hits:
        print(f"nothing matched '{args.company}'", file=sys.stderr)
        return 1
    hits.sort(key=lambda e: -(e.score or 0))
    e = hits[0]

    h = hist.load()
    transitions = [ev for ev in h.for_entry(e.url) if ev.kind == "status"]
    if len(transitions) < 1:
        print(f"no recorded transitions for {e.company} — nothing to undo", file=sys.stderr)
        return 1

    last_t = transitions[-1]
    # Preferred: the transition recorded where it came from ("from shortlisted").
    # Fallback: the destination of the transition before it. Last resort: new.
    import re as _re

    m = _re.match(r"from (\w+)", last_t.detail or "")
    if m and m.group(1) in STATUSES:
        prev = m.group(1)
    elif len(transitions) >= 2:
        prev = transitions[-2].text.replace("moved to", "").strip()
    else:
        prev = "review"

    last = last_t
    e.status = prev
    e.last_touched = today()
    h.add(e.url, "status", f"moved to {prev}", f"undo of: {last.text}")
    hist.save(h)
    feed.save(entries)
    print(f"{e.title} — {e.company}: undid '{last.text}' → back to '{prev}'")
    print("(the reverted transition stays in the log — history is append-only)")
    _refresh_dash()
    return 0


def schedule_cmd(args: argparse.Namespace) -> int:
    from . import schedule as sch

    if args.action == "on":
        print(sch.install(args.hour, args.minute))
    elif args.action == "off":
        print(sch.uninstall())
    else:
        print(sch.status())
    return 0


def resolve_cmd(args: argparse.Namespace) -> int:
    from . import resolve as rs

    entries = feed.load()
    pairs = rs.targets(entries, args.min_score)
    if not pairs:
        print("every founder on relevant entries already has a profile.")
        return 0
    path = rs.write_queue(pairs)
    print(f"{len(pairs)} founders need resolution — wrote {path.relative_to(ROOT)}")
    for e, f in pairs[:12]:
        print(f"  {e.score:>3}  {f['name']:<26} {f.get('title','')[:30]:<30} {e.company}")
    print("\nresolve in-session (or via the API once billing works), then:")
    print("  make apply-resolution FILE=<results.json>")
    return 0


def apply_resolution_cmd(args: argparse.Namespace) -> int:
    import json as _json

    from . import resolve as rs

    results = _json.loads(Path(args.file).expanduser().read_text(encoding="utf-8"))
    entries = feed.load()
    updated, rejected = rs.apply(entries, results)
    for r in rejected:
        print(f"  ✗ {r}", file=sys.stderr)
    if not args.dry_run:
        feed.save(entries)
        _refresh_dash()
    print(f"resolved {updated} profiles" + (f" ({len(rejected)} rejected)" if rejected else ""))
    return 0



def app_cmd(args: argparse.Namespace) -> int:
    from . import app

    app.serve(open_browser=not args.no_open)
    return 0


def track_cmd(args: argparse.Namespace) -> int:
    from . import entities

    rec = entities.track_company(args.company, args.why or "", args.site or "",
                                 linkedin=args.linkedin or "")
    print(f"tracking: {rec['name']}" + (f" — {rec.get('why','')}" if rec.get("why") else ""))
    _refresh_dash()
    return 0


def person_cmd(args: argparse.Namespace) -> int:
    from . import entities

    rec = entities.add_person(
        name=args.name, company=args.company or "", role=args.role or "",
        linkedin=args.linkedin or "", relationship=args.relationship,
        signal=args.signal or "", note=args.note or "",
        email=args.email or "", x=args.x or "", phone=args.phone or "",
    )
    print(f"person: {rec['name']}" + (f" @ {rec['company']}" if rec.get("company") else ""))
    if rec.get("signals"):
        print(f"  connections: {', '.join(rec['signals'])}")
    _clear_newsletter_lead(rec)
    _refresh_dash()
    return 0


def _clear_newsletter_lead(rec: dict) -> None:
    """Accepting a person clears their pending newsletter-lead queue entry."""
    import json as _json

    from .boards.substack import PEOPLE_QUEUE
    from .models import normalize_company

    try:
        rows = _json.loads(PEOPLE_QUEUE.read_text())
    except (FileNotFoundError, _json.JSONDecodeError):
        return
    key = (rec.get("name", "").lower().strip(), normalize_company(rec.get("company", "")))
    kept = [r for r in rows
            if (r.get("name", "").lower().strip(),
                normalize_company(r.get("company", ""))) != key]
    if len(kept) != len(rows):
        PEOPLE_QUEUE.write_text(_json.dumps(kept, indent=1))
        print("  cleared from the newsletter-leads queue")


def queue_fills_cmd(args: argparse.Namespace) -> int:
    """Queue LinkedIn profile fills for people at companies Eric has SAVED.

    Signals are only as good as the text they read, and for almost everyone
    that text is a headline. The fix is the Chrome sitting — but sweeping all
    220 people with a profile URL would spend twelve sittings on companies he
    has never acted on. Eric's call (2026-08-07): saved companies only.

    "Saved" means the explicit yes, either way he gives it — the company
    favourited (`tracked`), or any of its postings moved past review.
    """
    from . import entities, profiles
    from .models import normalize_company as _nc

    entries = feed.load()
    overlay = entities.load_company_overlay()
    tracked = {k for k, v in overlay.items() if v.get("tracked")}
    live = {_nc(e.company) for e in entries
            if e.status in ("saved", "applied", "interviewing", "offer")}
    wanted = tracked | live

    people = entities.people(entries)
    queued = already = done = skipped = 0
    for p in sorted(people, key=lambda x: -(x.best_score or 0)):
        if "linkedin.com/in/" not in (p.linkedin or ""):
            continue
        if _nc(p.company or "") not in wanted:
            continue
        # Founders only (Eric, 2026-08-07). An alum is already warm — the
        # alumni sweep verified them on their own education page — so reading
        # their whole profile buys nothing and costs a sitting slot.
        if p.relationship != "founder":
            skipped += 1
            continue
        if profiles.get(p.linkedin):
            done += 1
            continue
        rec = {"linkedin": p.linkedin, "name": p.name,
               "role": p.role, "location": p.location}
        if entities.queue_person_fill(rec):
            queued += 1
        else:
            already += 1

    print(f"\n{len(wanted)} saved companies")
    print(f"  queued for a profile fill : {queued}")
    print(f"  already in the queue      : {already}")
    print(f"  already swept             : {done}")
    print(f"  skipped (not a founder)   : {skipped}")
    print(f"\nThe sitting drains 10 per run, five runs a day — "
          f"about {max(1, -(-queued // 10))} sittings.")
    return 0


def signals_audit_cmd(args: argparse.Namespace) -> int:
    """Every connection signal on one screen, with the words that earned it.

    Precision is the only metric that matters here: a missed signal costs one
    lead, a false one costs a credibility-destroying email. Nothing was
    watching it drift. This is the readback — scan it, and anything wrong
    becomes a negative-control test pin.
    """
    from . import entities, profiles

    entries = feed.load()
    people = entities.people(entries)
    cos = entities.companies(entries)

    flagged = [p for p in people if p.signals]
    cached = profiles.load()
    print(f"\nSIGNAL AUDIT — {len(flagged)} of {len(people)} people carry a signal\n")

    from collections import Counter
    tally = Counter(s for p in flagged for s in p.signals)
    print("  " + "  ".join(f"{k}:{v}" for k, v in tally.most_common()) + "\n")

    for p in sorted(flagged, key=lambda x: x.name.lower()):
        if args.signal and args.signal not in p.signals:
            continue
        where = "profile" if profiles.get(p.linkedin) else "no profile on file"
        print(f"  {p.name[:26]:28} {', '.join(p.signals)}")
        print(f"      {p.role[:38] or '—':40} {p.company[:26]:28} [{where}]")
        for label in p.signals:
            why = p.evidence.get(label)
            print(f"      {label:14} {why[:104] if why else '(hand-recorded — no automatic evidence)'}")
        print()

    # Coverage is the other half: a signal that never fires because nothing was
    # captured looks identical to a person who genuinely has no tie.
    with_li = [p for p in people if "linkedin.com/in/" in (p.linkedin or "")]
    swept = [p for p in with_li if profiles.get(p.linkedin)]
    ov = entities.load_company_overlay()
    described = sum(1 for v in ov.values() if v.get("description"))
    print("COVERAGE — what the matcher could not see")
    print(f"  people with a profile URL:      {len(with_li)}")
    print(f"  …whose profile has been swept:  {len(swept)}"
          f"   <- the rest are matched on a headline alone")
    print(f"  companies with a description:   {described} of {len(ov)}"
          f"   <- employer inheritance is blind for the rest")
    print(f"  cached profiles on disk:        {len(cached)}")
    print("\nAnything wrong above is a negative control — paste it into a test pin.")
    return 0


def insights_cmd(args: argparse.Namespace) -> int:
    from . import importer, insights

    print(insights.render(feed.load(), importer.load_calibration()))
    return 0


def describe_cmd(args: argparse.Namespace) -> int:
    from . import describe as dsc

    entries = feed.load()
    stats = dsc.run(entries, limit=args.limit, min_score=args.min_score)
    print(f"{stats['missing']} companies lacked a description")
    print(f"  found deterministically: {stats['found']} (WaaS pages, site meta tags)")
    if stats["queued"]:
        print(f"  queued for web search:   {stats['queued']} → data/describe_queue.json")
        print("  fill in-session, then: make apply-descriptions FILE=<results.json>")
    sq = dsc.queue_summaries(entries, min_score=max(args.min_score, 65))
    print(f"  queued for 1-3 sentence summaries: {sq['queued']} → data/summarize_queue.json")
    _refresh_dash()
    return 0


def apply_descriptions_cmd(args: argparse.Namespace) -> int:
    import json as _json

    from . import describe as dsc

    n = dsc.apply(_json.loads(Path(args.file).expanduser().read_text(encoding="utf-8")))
    print(f"applied {n} descriptions")
    _refresh_dash()
    return 0


def doctor_cmd(args: argparse.Namespace) -> int:
    from . import doctor

    out = doctor.render(live=args.live)
    print(out)
    return 1 if "problem" in out.splitlines()[2] else 0


def enrich_cmd(args: argparse.Namespace) -> int:
    load_dotenv(ROOT / ".env")
    from . import enrich as en

    entries = feed.load()
    targets = [e for e in entries if en.needs_enrichment(e, args.min_score)]
    if args.company:
        from . import status as st

        wanted = {id(x) for x in st.find(entries, args.company)}
        targets = [e for e in targets if id(e) in wanted]
    if args.limit:
        targets = targets[: args.limit]

    if not targets:
        print("nothing needs enrichment.")
        return 0

    print(f"{len(targets)} entries scoring {args.min_score}+ with no founder on file")

    # Free pass first: the same company often appears several times, and only
    # some boards supply founders. Share what's already known before looking
    # anything up.
    shared = en.propagate_known(entries)
    if shared:
        print(f"  filled {shared} from other entries at the same company")
        targets = [e for e in targets if not e.founders]
        if not targets:
            if not args.dry_run:
                feed.save(entries)
            print()
            print("all covered without a single lookup.")
            return 0

    # Deterministic pass next — free, and often enough.
    found = 0
    for e in targets:
        site = en.company_domain(e)
        if not site:
            continue
        res = en.scrape_site(site)
        if res.founders and not e.founders:
            e.founders = res.founders
            found += 1
            print(f"  ✓ {e.company:<20} {len(res.founders)} founder(s) from {site}")
        if res.warm_path and not e.warm_path:
            e.warm_path = res.warm_path
            print(f"  ~ {e.company:<20} warm path: {res.warm_path[:60]}")

    remaining = [e for e in targets if not e.founders]
    if remaining:
        path = en.write_queue(remaining, args.min_score)
        print()
        print(f"{len(remaining)} still need a lookup — wrote {path.relative_to(ROOT)}")
        print("  fill it in-session, then: make apply-enrichment FILE=<results.json>")

    if not args.dry_run:
        feed.save(entries)
    print()
    print(f"enriched {found} from company sites")
    return 0


def apply_enrichment_cmd(args: argparse.Namespace) -> int:
    import json as _json

    from . import enrich as en

    results = _json.loads(Path(args.file).expanduser().read_text(encoding="utf-8"))
    entries = feed.load()
    n = en.apply(entries, results)
    if not args.dry_run:
        feed.save(entries)
    print(f"applied enrichment to {n} entries from {len(results)} companies")
    return 0


def _refresh_dash() -> None:
    """Rebuild the dashboard after any state change.

    It's a static file, so without this every mark/log/undo leaves it showing
    yesterday's pipeline — worse than no dashboard, because it looks current.
    Sub-second, so it's not worth a flag.
    """
    try:
        from . import dashboard

        dashboard.build(feed.load())
    except Exception:  # noqa: BLE001 — a render bug must not block a state change
        pass


def status_cmd(args: argparse.Namespace) -> int:
    from . import status as st

    print(st.render(feed.load(), limit_new=args.limit))
    return 0


def mark_cmd(args: argparse.Namespace) -> int:
    from . import importer
    from . import status as st

    entries = feed.load()
    # capture pre-transition states for the history record (and undo)
    probe = st.find(entries, args.url)
    prior_states = [e.status for e in probe]
    changed, msg = st.mark(entries, args.url, args.status, args.note)
    if not changed:
        print(msg, file=sys.stderr)
        return 1

    # A transition with a reason is a labeled example — the scorer should learn it.
    recorded = 0
    if args.note:
        cal = importer.load_calibration()
        for e in changed:
            cal.append(st.calibration_record(e, args.note))
            recorded += 1
        importer.save_calibration(cal)

    from . import history as hist
    h = hist.load()
    for e, prior in zip(changed, prior_states):
        detail = f"from {prior}" + (f" — {args.note}" if args.note else "")
        h.add(e.url, "status", f"moved to {e.status}", detail)
    hist.save(h)

    feed.save(entries)
    print(msg)
    for e in changed:
        print(f"  {e.title} — {e.company}  →  {e.status}")
    _refresh_dash()
    if recorded:
        print(f"  recorded {recorded} calibration example(s); the scorer will use them")
    return 0


def alumni_cmd(args: argparse.Namespace) -> int:
    """The school-sweep worklist: which companies to check, at which URLs.

    The sweep itself is a browser task (LinkedIn is login-walled), so this
    prints the exact URLs to walk — one per school per company — skipping
    whatever was already checked. Slugs are stored in the company overlay the
    first time they're found; a company without one gets a search URL instead.

    Recipe per hit, non-negotiable: open the profile, confirm the school in
    THEIR OWN education or experience, then record via `make person ...
    SIGNAL=Emory` (or NMH). The keyword match alone is not evidence — 'Emory'
    matches people who merely worked at Emory Healthcare, and a false
    same-school claim could end up in an email.
    """
    from urllib.parse import quote

    from . import entities, funding

    schools = {"emory": "Emory", "nmh": "Northfield Mount Hermon",
               "jets": "New York Jets"}
    entries = feed.load()
    names = funding.watch_list(entries, min_score=args.min_score)
    overlay = entities.load_company_overlay()

    todo, need_slug, done, blocked = [], [], 0, 0
    for name in sorted(names):
        rec = overlay.get(normalize_company(name), {})
        if rec.get("sweep_blocked") or rec.get("not_interested"):
            blocked += 1
            continue
        checked = rec.get("alumni_checked", {})
        pending = [s for s in schools if s not in checked]
        if not pending:
            done += 1
            continue
        li = rec.get("linkedin", "")
        if li:
            for school in pending:
                todo.append((name, f"{li}/people/?keywords={quote(schools[school])}"))
        else:
            need_slug.append(name)

    print(f"\nALUMNI SWEEP — {len(names)} companies watched, {done} fully checked"
          + (f", {blocked} unsweepable" if blocked else "") + "\n")
    if todo:
        print("Ready to sweep (slug on file):")
        for name, url in todo:
            print(f"  {name:28} {url}")
    if need_slug:
        print("\nNeed their LinkedIn page first (find once, store with"
              " `make track COMPANY=x LINKEDIN=url`):")
        for name in need_slug:
            print(f"  {name:28} https://www.linkedin.com/search/results/companies/?keywords={quote(name)}")
    print("\nVerify every hit on their own profile before recording. Chips appear"
          "\non the company and posting cards automatically once a person is saved.")
    return 0


def funding_cmd(args: argparse.Namespace) -> int:
    """Flag companies that just raised — the best moment to write to a founder."""
    from . import funding

    entries = feed.load()
    names = funding.watch_list(entries, min_score=args.min_score)
    if args.limit:
        names = names[: args.limit]
    print(f"\nChecking {len(names)} watched companies for new Form D filings…")

    raises, state = funding.check(names)
    print(funding.render(raises, len(names)))

    if raises and not args.dry_run:
        touched = funding.apply(entries, raises)
        feed.save(entries)
        funding.save_state(state)
        print(f"\nRecorded on {touched} entries.\n")
    elif not args.dry_run:
        funding.save_state(state)
    if not args.dry_run:
        stamped = funding.stamp_overlay(state)
        if stamped:
            print(f"Raise dates: {stamped} companies stamped with their latest Form D.")
        print()
    return 0


def founders_cmd(args: argparse.Namespace) -> int:
    """Fill missing founders from SEC Form D officer records.

    Work at a Startup is the only board that ships founders; everything else
    leaves a company with no human to write to. Any US startup that has raised
    has its officers on public record, so this closes most of that gap for free
    and exactly — with the hard rule that an ambiguous match is reported, never
    written.
    """
    import time

    from . import entities
    from . import history as hist
    from .boards.edgar import lookup_founders

    entries = feed.load()
    views = entities.companies(entries)
    targets = [
        c for c in views
        if not c.founders
        and (c.best_score or 0) >= args.min_score
        and any(p["status"] != "uninterested" for p in c.postings)
    ]
    targets.sort(key=lambda c: -(c.best_score or 0))
    targets = targets[: args.limit]

    if not targets:
        print(f"\nEvery company scoring {args.min_score}+ already has a founder.\n")
        return 0

    print(f"\nLooking up {len(targets)} companies in SEC Form D records…\n")
    confirmed, probable, missed = [], [], []
    for c in targets:
        res = lookup_founders(c.name)
        names = ", ".join(p["name"] for p in res["founders"][:3])
        if res["confidence"] == "confirmed":
            confirmed.append((c, res))
            print(f"  ✓ {c.name:28} {names}")
        elif res["confidence"] == "probable":
            probable.append((c, res))
            print(f"  ? {c.name:28} {names}  — {res['note']}")
        else:
            missed.append(c)
        time.sleep(0.4)   # SEC asks for <10 req/s; this is far under

    if missed:
        print(f"\n  no filing matched: {', '.join(c.name for c in missed[:12])}")

    if not args.apply:
        print(
            f"\n{len(confirmed)} confirmed, {len(probable)} probable, {len(missed)} missed."
            f"\nRe-run with --apply to write the confirmed ones into the feed."
            f"\nProbable matches are never written automatically — an old filing often "
            f"belongs to a different company with the same name.\n"
        )
        return 0

    written = 0
    for c, res in confirmed:
        for e in entries:
            if normalize_company(e.company) == c.key and not e.founders:
                e.founders = res["founders"]
                e.last_touched = today()
                written += 1
        hist.record(
            c.postings[0]["url"] if c.postings else c.name,
            "enrich",
            f"founders from SEC Form D: {', '.join(p['name'] for p in res['founders'][:3])}",
            res["evidence"].get("filing", ""),
        )
    feed.save(entries)
    print(f"\nWrote founders to {written} entries across {len(confirmed)} companies.")
    if probable:
        print(f"{len(probable)} probable matches left for you to confirm by hand.\n")
    return 0


def sends_cmd(args: argparse.Namespace) -> int:
    """Who to write to today, why now, and through which channel."""
    from . import timing

    entries = feed.load()
    q = timing.queue(entries, limit=args.limit, min_score=args.min_score)
    print()
    print(timing.render(q))
    print()
    return 0


def followups_cmd(args: argparse.Namespace) -> int:
    from . import status as st

    entries = feed.load()
    due = st.followups(entries)
    if not due:
        print("\nNobody is owed a nudge today.\n")
        return 0

    print(f"\n{'─' * 72}\nFOLLOW-UPS DUE ({len(due)})\n{'─' * 72}\n")
    for e, age, n in due:
        f = (e.founders or [{}])[0]
        print(f"  nudge #{n}  ·  {age}d quiet  ·  {e.title} — {e.company}")
        if f.get("name"):
            print(f"      {f['name']} {f.get('linkedin', '')}")
        print(f"      {e.url}")
        print(
            "      Keep it under 60 words, shorter than the first email, and add one "
            "new thing\n      rather than restating it."
        )
        print()
    print("Two nudges is the whole budget — past that it stops reading as persistence.\n")
    return 0


def intros_cmd(args: argparse.Namespace) -> int:
    from . import intros

    conns = intros.load_connections()
    entries = [e for e in feed.load() if (e.score or 0) >= args.min_score]
    matches = intros.find_paths(entries, conns) if conns else []
    print(intros.render(matches, len(conns), args.min_score))

    if conns and not matches:
        pool = intros.alumni_pool(conns)
        if pool:
            print("Shared-institution people in your network (useful for cold companies):")
            for label, people in pool.items():
                print(f"  {label}: {len(people)}")
            print()
    return 0


def brief_cmd(args: argparse.Namespace) -> int:
    from . import brief as bf
    from . import status as st

    entries = feed.load()
    hits = st.find(entries, args.company)
    if not hits:
        print(f"nothing matched '{args.company}'", file=sys.stderr)
        return 1
    hits.sort(key=lambda e: -(e.score or 0))
    path = bf.generate(hits[0], args.description or hits[0].why)
    from . import history as hist
    hist.record(hits[0].url, "artifact", "interview brief written", str(path.relative_to(ROOT)))
    print(f"wrote {path.relative_to(ROOT)}")
    print(f"  {hits[0].title} — {hits[0].company}")
    return 0



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pipeline")
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scout", help="fetch, score, and append to data/feed.json")
    s.add_argument("--board", action="append", help="limit to one board (repeatable)")
    s.add_argument("--dry-run", action="store_true", help="do not write the feed")
    s.add_argument("--limit", type=int, help="cap postings sent to the scorer")
    s.add_argument("--no-funding", action="store_true",
                   help="skip the post-scout funding watch")
    s.set_defaults(func=scout)

    i = sub.add_parser("import", help="import a shortlist spreadsheet")
    i.add_argument("--file", required=True, help="path to the .xlsx tracker")
    i.add_argument("--source", default="shortlist", help="value for the feed's source field")
    i.add_argument("--dry-run", action="store_true")
    i.set_defaults(func=import_tracker)


    ap_p = sub.add_parser("apply", help="write any in-session result file back (auto-detects type)")
    ap_p.add_argument("--file", required=True)
    ap_p.add_argument("--as", dest="as_kind", default="",
                      choices=["scores", "enrichment", "descriptions", "resolution"],
                      help="force the type when the file is ambiguous")
    ap_p.add_argument("--dry-run", action="store_true")
    ap_p.set_defaults(func=apply_cmd)

    sb = sub.add_parser("substack-backfill",
                        help="one-time archive seed of the substack source")
    sb.add_argument("--months", type=int, default=3)
    sb.add_argument("--dry-run", action="store_true")
    sb.set_defaults(func=substack_backfill_cmd)

    xw = sub.add_parser("xsweep", help="X hiring-tweet sweep via Grok (needs XAI_API_KEY)")
    xw.add_argument("--force", action="store_true",
                    help="run even if today's capped call already happened")
    xw.add_argument("--lens", default="",
                    help='bias one sweep toward a slice, e.g. "NYC" or "health and fitness"')
    xw.set_defaults(func=xsweep_cmd)

    lv = sub.add_parser("levels",
                        help="backfill the seniority level on postings that have text (API)")
    lv.add_argument("--limit", type=int, help="cap the batch")
    lv.add_argument("--all", action="store_true", help="redo levels already set")
    lv.set_defaults(func=levels_cmd)

    rp = sub.add_parser("repopulate",
                        help="bulk-fetch missing posting text and expire dead links (no tokens)")
    rp.add_argument("--limit", type=int, default=300, help="cap the batch")
    rp.add_argument("--all-liveness", action="store_true",
                    help="check every review posting's page, not just descriptionless ones")
    rp.set_defaults(func=repopulate_cmd)

    mi = sub.add_parser("mirror",
                        help="recover walled posting text by finding the role mirrored elsewhere (API)")
    mi.add_argument("--limit", type=int, default=60, help="cap the batch")
    mi.add_argument("--max-spend", type=float, default=5.0, help="refuse runs above this ($)")
    mi.add_argument("--yes", action="store_true", help="skip the spend guard")
    mi.set_defaults(func=mirror_cmd)

    rs = sub.add_parser("rescore", help="score entries already in the feed")
    rs.add_argument("--all", action="store_true", help="rescore even already-scored entries")
    rs.add_argument("--company", help="limit to one company")
    rs.add_argument("--limit", type=int, help="cap the batch")
    rs.add_argument("--max-spend", type=float, default=5.0, help="refuse runs above this ($)")
    rs.add_argument("--yes", action="store_true", help="skip the spend guard")
    rs.add_argument("--dry-run", action="store_true")
    rs.set_defaults(func=rescore_cmd)

    en_p = sub.add_parser("enrich", help="find founders for high-scoring postings")
    en_p.add_argument("--min-score", type=int, default=75)
    en_p.add_argument("--company")
    en_p.add_argument("--limit", type=int)
    en_p.add_argument("--dry-run", action="store_true")
    en_p.set_defaults(func=enrich_cmd)


    td_p = sub.add_parser("today", help="the morning view: what to do right now")
    td_p.set_defaults(func=today_cmd)

    un = sub.add_parser("undo", help="revert an entry's last status change")
    un.add_argument("--company", required=True)
    un.set_defaults(func=undo_cmd)

    lg = sub.add_parser("log", help="show or add to an entry's activity history")
    lg.add_argument("--company", required=True)
    lg.add_argument("--note", help="append a timestamped note instead of showing the log")
    lg.set_defaults(func=log_cmd)

    sc_p = sub.add_parser("schedule", help="daily automatic scout (launchd)")
    sc_p.add_argument("action", choices=["on", "off", "status"])
    sc_p.add_argument("--hour", type=int, default=8)
    sc_p.add_argument("--minute", type=int, default=0)
    sc_p.set_defaults(func=schedule_cmd)


    rv = sub.add_parser("resolve", help="queue founders who need LinkedIn profiles")
    rv.add_argument("--min-score", type=int, default=70)
    rv.set_defaults(func=resolve_cmd)


    ap = sub.add_parser("app", help="run the local web app (dashboard + actions)")
    ap.add_argument("--no-open", action="store_true")
    ap.set_defaults(func=app_cmd)

    tr = sub.add_parser("track", help="track an interesting company (with or without a posting)")
    tr.add_argument("--linkedin", default="")
    tr.add_argument("--company", required=True)
    tr.add_argument("--why")
    tr.add_argument("--site")
    tr.set_defaults(func=track_cmd)

    pe = sub.add_parser("person", help="add or update a person (founder, contact, networking)")
    pe.add_argument("--email", default="")
    pe.add_argument("--x", default="")
    pe.add_argument("--phone", default="")
    pe.add_argument("--name", required=True)
    pe.add_argument("--company")
    pe.add_argument("--role")
    pe.add_argument("--linkedin")
    pe.add_argument("--relationship", default="networking")
    pe.add_argument("--signal", help="connection to Eric, e.g. 'Emory', 'NMH', 'Atlanta'")
    pe.add_argument("--note")
    pe.set_defaults(func=person_cmd)

    de = sub.add_parser("describe", help="get a concise what-it-does line for every company")
    de.add_argument("--limit", type=int)
    de.add_argument("--min-score", type=int, default=0)
    de.set_defaults(func=describe_cmd)


    qf = sub.add_parser("queue-fills", help="queue profile fills for saved companies")
    qf.set_defaults(func=queue_fills_cmd)

    sa = sub.add_parser("signals-audit", help="every connection signal + its evidence")
    sa.add_argument("--signal", help="only this label, e.g. Sports")
    sa.set_defaults(func=signals_audit_cmd)

    ins = sub.add_parser("insights", help="what the app has learned from you — and where it's wrong")
    ins.set_defaults(func=insights_cmd)

    dr = sub.add_parser("doctor", help="health check: data, env, credentials, boards")
    dr.add_argument("--live", action="store_true",
                    help="also fetch each board and make one tiny API call")
    dr.set_defaults(func=doctor_cmd)


    st_p = sub.add_parser("status", help="show the live pipeline")
    st_p.add_argument("--limit", type=int, default=12, help="how many 'new' rows to show")
    st_p.set_defaults(func=status_cmd)

    m = sub.add_parser("mark", help="move an entry to a new status")
    m.add_argument("--url", required=True, help="URL fragment, 'Company', or 'Company:Title'")
    m.add_argument("--status", required=True, choices=list(STATUSES))
    m.add_argument("--note", default="", help="why — recorded as a calibration example")
    m.set_defaults(func=mark_cmd)

    al = sub.add_parser("alumni", help="school-sweep worklist (Emory, NMH) with URLs")
    al.add_argument("--min-score", type=int, default=65)
    al.set_defaults(func=alumni_cmd)

    fn = sub.add_parser("funding", help="flag watched companies that just raised")
    fn.add_argument("--min-score", type=int, default=65)
    fn.add_argument("--limit", type=int, default=0, help="cap companies checked")
    fn.add_argument("--dry-run", action="store_true")
    fn.set_defaults(func=funding_cmd)

    fd = sub.add_parser("founders", help="fill missing founders from SEC Form D officers")
    fd.add_argument("--min-score", type=int, default=65)
    fd.add_argument("--limit", type=int, default=25)
    fd.add_argument("--apply", action="store_true", help="write confirmed matches to the feed")
    fd.set_defaults(func=founders_cmd)

    sn = sub.add_parser("sends", help="who to contact now, why now, and how")
    sn.add_argument("--limit", type=int, default=10)
    sn.add_argument("--min-score", type=int, default=65)
    sn.set_defaults(func=sends_cmd)

    fu = sub.add_parser("followups", help="who has gone quiet")
    fu.set_defaults(func=followups_cmd)

    it = sub.add_parser("intros", help="warm paths via your LinkedIn connections")
    it.add_argument("--min-score", type=int, default=70)
    it.set_defaults(func=intros_cmd)

    br = sub.add_parser("brief", help="interview prep brief")
    br.add_argument("--company", required=True)
    br.add_argument("--description", default="")
    br.set_defaults(func=brief_cmd)


    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
