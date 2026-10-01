# DESIGN.md — the design constitution

Every pixel in `pipeline/dashboard.py` answers to this file. New UI work picks
values **only** from these scales; if a needed value isn't here, the scale gets
amended here first (one-line commit), then used. `make test` enforces the
mechanical parts (token guard → design lint); taste calls below are law until
Eric changes them. Companion docs: `docs/design-audit.md` (why each rule
exists), `docs/design-research.md` (where it came from).

## Identity

Earthy and warm — moss, clay, amber on near-black loam; dark-first with a full
light map. Serif display (`--serif`) for names and titles, sans (`--sans`) for
data. Apple liquid-glass surfaces (translucent header, glass fabs), leaf
ornaments on section heads. Gamified *in structure* (streaks, goals, sfx), never
in decoration (no emoji showers, no confetti). Deliberately dense: this is a
working tool driven daily, not a marketing page.

**Hierarchy comes from de-emphasis.** The quiet card is the default; meaning is
carried by the score ring, the warm/signal chips, and type weight — never by
making a card louder. One accent surface per view, maximum.

## Tokens

Color slots (values live in the `:root` block of `dashboard.py` + its
`[data-theme="light"]` override map — one block each, no hex/rgba/hsla literals
anywhere else):

| Slot | Role |
|---|---|
| `--bg` / `--card` | page / raised surface |
| `--on-solid` | ink on any saturated fill (accent, kill, hot solids) |
| `--ink` / `--muted` | primary / secondary text |
| `--line` / `--line2` | hairline / emphasized edge |
| `--accent` | brand moss — interactive affordance |
| `--hot` | success/high-score mint (ink on it follows the fill, not the theme) |
| `--warn` / `--kill` | amber caution / clay negative |
| `--hdr-bg` `--hdr-ink` `--sub-bg` | header family |
| `--a0…--a6` | white-alpha ramp (0/.08/.16/.24/.32/.42/.92) — every glass & plastic highlight |
| `--b1…--b4` | black-alpha ramp (.08/.15/.32/.45) — scrims, floor shadows, modal backdrop |
| `--mossh --goldh --clayh --redh --emberh --minth --skyh --roseh` | named hues; every `hsl()` in a rule takes its hue from one of these (or `--indh`/`--ageh`/`--sh` data hues) |
| `--sig-*` (alumni, nmh, sports, music, neuro, inst, founder, conn, recruiter, family) | the ten signal-chip washes |

Signal hues (the coordinated identity of chips; used only via these tokens):
**alumni 217°** (school blues), **opportunity/gold 43°**, **connection/clay
16°**, warm-family tokens (`--founder-*`, `--warmlead-*`, `--emory-*`,
`--nmh-*`). Industry tint on cards/groups uses **one formula**:
`hsl(var(--indh) …)`. Two variants, and the split is *kind-coding* (Eric,
2026-08-09): **posting cards wear the two-hue `indGrad` gradient** (industry
pours from the top, role rises from the bottom — a posting reads as a posting
at a glance), **company/person surfaces wear the single-hue `indShade` wash**.
No third variant.

Scales — pick from these, never between them:

- **Spacing**: even 2px grid to 16 (`2 4 6 8 10 12 14 16`), then
  `20 24 32 48`. Odd values and off-grid stragglers (18, 22, 26, 30, 34) are
  banned in padding/margin/gap; positioning offsets and layout constants >34
  are exempt. `--gap-chip: 4px` for all chip rows.
- **Type**: `--t-xs 11 / --t-sm 12 / --t-md 14 / --t-lg 16 / --t-xl 24`.
  No `calc()` multipliers off the scale. Weights: `400 / 600 / 700`, `800`
  reserved for serif display. Tabular numerals on all counts.
- **Radius**: `--r-sm 6 / --r-md 10 / --r-pill` + `50%` for true circles. Frozen.
- **Elevation**: surface step first (`--card`), then the shadow ladder
  `--sh-rest / --sh-raised / --sh-overlay / --sh-modal` (theme-mapped: dark's
  raised value carries its own hairline ring) + `--ring` (Linear-style
  `0 0 0 1px` edge). Semantic accent glows (fresh-posting ring,
  tab-slide glow, drag-target ring) are allowlisted effects, not elevation.
- **Z-layers**: `--z-raised 10` (sticky header) / `--z-fab 60` (floating
  chrome band, small +N offsets order within it) / `--z-modal 80` /
  `--z-top 99` (splash, toast, skip-link). Component-local ordinals 0–7
  inside a stacking context are exempt.
- **Motion**: `--dur-1 120ms / --dur-2 200ms / --dur-3 320ms`; easings: the
  `ease` keyword (default) and `--ease-spring` (the one spring). Ambient loops
  (logo horn, shimmer, ≥0.9s) are exempt from the duration scale. Anything
  animating on *every* card obeys `--dur-1`.

## Components — one definition each

- **Chip** (`.cchip` family): one height `--chip-h: 22px`, pad `2px 10px`,
  `--t-xs`, pill. *Every* chip-family member conforms — `rolechip`, `misspill`,
  counters included. A chip row never contains two heights.
- **Button**: default 28px (`--t-sm`); large 32px for primary page actions
  (fabs excepted). The **triage ✓/✗ control is 28px everywhere** — tracker,
  postings, tweets, companies, rolodex.
- **Tile** (stat row): one shape — rect, `--r-md`, pad `6px 12px`. The streak
  pills adopt it; "pill vs rect" no longer encodes anything.
- **Cards**: `postingCard` / `personCard` / `companyCard` / `tweetCard` /
  `cogroup` — one definition per kind (standing invariant), tracker renders the
  same functions. Padding per kind is fixed: `ccard 8/16/12/16`,
  `pcard 8/10/8/52`, `cogroup 12/12/14/12`. Card surface = `--card` + the
  kind-coded formula above (gradient = posting, wash = company/person); no
  score-hue gradients.
- **Section heads**: `.tsec` (serif 16/700, uppercase, +0.6 tracking, leaf +
  hairline) for page sections; `.cohead` (serif 16/600, no transform) for
  groups. No third voice.
- **Empty states**: one grammar — dashed `--line` border, `--muted` label;
  state color may tint the label, never the border style.
- **Banners**: one component, `--warn` or `--kill` tint via the industry-alpha
  formula; max two visible, they collapse into one line each.

## Restraint rules (survived iteration — do not relitigate)

1. Hover changes shadow/border only — never transform (transforms shimmer).
2. Pagination over infinite scroll; expanded cards fetch detail lazily.
3. No emoji showers; ornament budget is the leaf + the horn logo. Data
   glyphs are different: 🔥 (warm), 🎓 (multi-school alumni), 🦅 (Emory),
   🐗 (NMH — the Hoggers), 🌱/🌳 (Pre Seed/Seed — letters $A/$B+ where no
   metaphor exists), 💰 (raised), and the city set — 🗽/🌉/🍀/🌐 where
   unambiguous, country flag + letters for Europe (🇩🇪 BER, since
   Berlin/Munich share a flag) — are
   abbreviations with the word in the tooltip (Eric, 2026-08-12), not
   decoration. Glyphs replace words on data chips only — filter chips and
   dropdowns are controls, and controls speak in words.
4. Cross-origin embeds mount within 700px of viewport, then persist for
   the session — the pane renders once, so embeds never reload (bounded
   by the ledger size; revisit with a cap past ~100 tweets).
5. Fixed overlays (shortcut hint, fabs) own reserved space — nothing under them.

## Enforcement

All lint arms are live in `tests/test_regressions.py` (2026-08-09): no raw
color literal outside the token blocks (`test_no_raw_color_literals…`), spacing
on the grid (`test_spacing_sits_on_the_even_grid`), elevation/z/motion on
tokens (`test_elevation_z_and_motion_run_on_tokens`), one chip geometry
(`test_chip_family_shares_one_height`), fills-not-borders
(`test_data_chips_are_tinted_fills…`). A violation is a failing test that
names this file. JS-generated colors (cityTone, indGrad/indShade, roleChip,
personTone) are the sanctioned data-driven formulas.

## Decided (Eric, 2026-08-09)

- **A. Plastic demoted (2026-08-11; supersedes the 08-09 call).** The
  injection-moulded rendering was judged over-engineered: the rolodex now
  wears the quiet wash + hairline + one top highlight like every other
  surface. The card-file *structure* stays — company groups, divider-tab
  headers, avatar rails, the alpha rail.
- **B. Data chips are tinted fills; borders mean interactive.** Filters,
  quick-edit, and the miss-pill keep border+fill; industry/city/stage/date
  chips drop the border. First card mocked for sign-off before the sweep
  applies it everywhere.
- **C. The header stays dark in light mode** — badges and active-tab ink get
  contrast-correct tokens instead.
