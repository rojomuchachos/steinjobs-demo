PY := ./.venv/bin/python

.PHONY: help setup test scout scout-dry rescore repopulate mirror levels substack-backfill xsweep enrich import apply status mark alumni founders funding sends followups intros brief

help:
	@echo ""
	@echo "  WORK THE PIPELINE"
	@echo "    make today                  what to do right now"
	@echo "    make sends                  who to write to, why now, through which channel"
	@echo "    make app                    the web app — everything, in three tabs"
	@echo "    make status                 the funnel, grouped by state"
	@echo "    make mark URL=x STATUS=y NOTE=z   move an entry; NOTE teaches the scorer"
	@echo "    make followups              who has gone quiet"
	@echo "    make intros                 warm paths via your LinkedIn connections"
	@echo ""
	@echo "  FIND THINGS"
	@echo "    make scout                  fetch, score, append (also runs the funding watch)"
	@echo "    make funding                who just raised — the best moment to write"
	@echo "    make founders               fill missing founders from SEC Form D"
	@echo "    make enrich / describe      founders + one-line company descriptions"
	@echo "    make track COMPANY=x WHY=y  follow a company, posting or not"
	@echo "    make person NAME=x ...      add a networking contact"
	@echo ""
	@echo "  RESEARCH ONE COMPANY"
	@echo "    make brief COMPANY=x        interview prep, once they reply"
	@echo "    make log COMPANY=x          activity history; NOTE=... to append"
	@echo ""
	@echo "  HOUSEKEEPING"
	@echo "    make test / doctor          regression suite; health check (LIVE=1 probes boards)"
	@echo "    make insights               where the scorer disagrees with you"
	@echo "    make signals-audit          every connection signal + the words that earned it"
	@echo "    make queue-fills            queue LinkedIn profile fills for saved companies"
	@echo "    make rescore                score the backlog"
	@echo "    make apply FILE=x.json      write any in-session result back (KIND= to force)"
	@echo "    make schedule / unschedule  daily 8am scout via launchd"
	@echo "    make undo COMPANY=x         revert the last status change"
	@echo ""
	@echo "  BOARD=<name> limit to one board   LIMIT=<n> cap work   MIN=<n> score floor"
	@echo ""

setup:
	python3 -m venv .venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -e .
	@echo "done. cp .env.example .env and add ANTHROPIC_API_KEY to enable scoring."

scout:
	@$(PY) -m pipeline.cli scout \
		$(if $(BOARD),--board $(BOARD)) \
		$(if $(LIMIT),--limit $(LIMIT))

scout-dry:
	@$(PY) -m pipeline.cli scout --dry-run \
		$(if $(BOARD),--board $(BOARD)) \
		$(if $(LIMIT),--limit $(LIMIT))

apply:
	@$(PY) -m pipeline.cli apply --file "$(FILE)" $(if $(KIND),--as $(KIND))

import:
	@$(PY) -m pipeline.cli import --file "$(FILE)"


status:
	@$(PY) -m pipeline.cli status $(if $(LIMIT),--limit $(LIMIT))

mark:
	@$(PY) -m pipeline.cli mark --url "$(URL)" --status "$(STATUS)" --note "$(NOTE)"

alumni:
	@$(PY) -m pipeline.cli alumni $(if $(MIN),--min-score $(MIN))

funding:
	@$(PY) -m pipeline.cli funding $(if $(MIN),--min-score $(MIN)) $(if $(LIMIT),--limit $(LIMIT)) $(if $(DRY),--dry-run)

founders:
	@$(PY) -m pipeline.cli founders $(if $(MIN),--min-score $(MIN)) $(if $(LIMIT),--limit $(LIMIT)) $(if $(APPLY),--apply)

sends:
	@$(PY) -m pipeline.cli sends $(if $(LIMIT),--limit $(LIMIT)) $(if $(MIN),--min-score $(MIN))

followups:
	@$(PY) -m pipeline.cli followups

intros:
	@$(PY) -m pipeline.cli intros $(if $(MIN),--min-score $(MIN))

brief:
	@$(PY) -m pipeline.cli brief --company "$(COMPANY)"


levels:
	@$(PY) -m pipeline.cli levels $(if $(LIMIT),--limit $(LIMIT)) $(if $(ALL),--all)

repopulate:
	@$(PY) -m pipeline.cli repopulate $(if $(LIMIT),--limit $(LIMIT)) $(if $(ALL),--all-liveness)

mirror:
	@$(PY) -m pipeline.cli mirror $(if $(LIMIT),--limit $(LIMIT)) $(if $(MAXSPEND),--max-spend $(MAXSPEND)) $(if $(YES),--yes)

rescore:
	@$(PY) -m pipeline.cli rescore \
		$(if $(COMPANY),--company "$(COMPANY)") \
		$(if $(LIMIT),--limit $(LIMIT)) \
		$(if $(MAXSPEND),--max-spend $(MAXSPEND)) \
		$(if $(YES),--yes)


substack-backfill:
	@$(PY) -m pipeline.cli substack-backfill $(if $(MONTHS),--months $(MONTHS))

xsweep:
	@$(PY) -m pipeline.cli xsweep $(if $(FORCE),--force) $(if $(LENS),--lens "$(LENS)")

enrich:
	@$(PY) -m pipeline.cli enrich $(if $(COMPANY),--company "$(COMPANY)") $(if $(LIMIT),--limit $(LIMIT)) $(if $(MIN),--min-score $(MIN))


test:
	@$(PY) -m pytest tests/ -q

log:
	@$(PY) -m pipeline.cli log --company "$(COMPANY)" $(if $(NOTE),--note "$(NOTE)")

today:
	@$(PY) -m pipeline.cli today

undo:
	@$(PY) -m pipeline.cli undo --company "$(COMPANY)"

schedule:
	@$(PY) -m pipeline.cli schedule on $(if $(HOUR),--hour $(HOUR))

unschedule:
	@$(PY) -m pipeline.cli schedule off

doctor:
	@$(PY) -m pipeline.cli doctor $(if $(LIVE),--live)

resolve:
	@$(PY) -m pipeline.cli resolve



app:
	@$(PY) -m pipeline.cli app

# Same app, reachable from a phone on the same wifi. No auth, and the API
# can change your data — trusted networks only. `make app` stays loopback.
app-lan:
	@JOBS_HOST=0.0.0.0 $(PY) -m pipeline.cli app --no-open

track:
	@$(PY) -m pipeline.cli track --company "$(COMPANY)" $(if $(WHY),--why "$(WHY)") $(if $(SITE),--site "$(SITE)") $(if $(LINKEDIN),--linkedin "$(LINKEDIN)")

person:
	@$(PY) -m pipeline.cli person --name "$(NAME)" $(if $(COMPANY),--company "$(COMPANY)") $(if $(ROLE),--role "$(ROLE)") $(if $(LINKEDIN),--linkedin "$(LINKEDIN)") $(if $(SIGNAL),--signal "$(SIGNAL)") $(if $(NOTE),--note "$(NOTE)")

queue-fills:
	@$(PY) -m pipeline.cli queue-fills

signals-audit:
	@$(PY) -m pipeline.cli signals-audit $(if $(SIGNAL),--signal "$(SIGNAL)")

insights:
	@$(PY) -m pipeline.cli insights

describe:
	@$(PY) -m pipeline.cli describe $(if $(LIMIT),--limit $(LIMIT)) $(if $(MIN),--min-score $(MIN))


mac:  ## build + launch the native Mac wrapper (real Liquid Glass on macOS 26)
	swiftc -O macapp/Sources/JobDestroyer/main.swift -o macapp/build/JobDestroyer -framework AppKit -framework WebKit
	./macapp/build/JobDestroyer &

# Fork a public-safe demo (real code + public postings, synthetic people and
# pipeline) into ../SteinJobs-Demo. Fails closed on its own leak audit.
