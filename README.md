# SteinJobs — demo

SteinJobs finds early-stage startup roles, the companies behind them, and the
people worth writing to. This fork runs the real app on a synthetic dataset.

**What's real:** the code, and the job postings (public listings from startup
job boards, with their scores and fit lines).
**What's fictional:** every person, every founder name, and the pipeline stages.

## Run it

```bash
make app
```

Opens a local web app at http://127.0.0.1:8377 (Python 3.11+, no dependencies).
Or open `data/dashboard.html` directly — a static snapshot with the action
buttons hidden.

## Tabs

- **Tracker** — kanban boards for roles and people, daily goals, streak.
- **Postings** — the scored pile, filterable, each with a "why this score" panel.
- **Tweets** — hiring tweets, embedded.
- **Companies** — derived from the postings, with a Following view.
- **Rolodex** — contacts, with warm-path signal chips.

Scouting (`make scout`) and scoring need board access and an API key; the
demo ships with data already in place so none of that is required.
