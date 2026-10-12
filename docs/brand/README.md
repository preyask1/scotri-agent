# Scotri — brand mark

The mark is an **open lens**: an eye drawn as two arcs that never quite meet, with one solid point
at dead centre.

| Element | What it carries |
|---|---|
| The aperture | Scotri's entire job is to look — across every repo, at Prod, at the live data behind them. |
| The open seam | The two arcs are 180°-rotationally symmetric and deliberately leave two gaps. The loop never closes: the work stops at paper, it never becomes a write. |
| The pupil | The single verified finding a whole investigation lands on. It is the only filled element in the mark. |

## Files

| File | Use |
|---|---|
| `scotri-mark.svg` | The mark, `currentColor`. Default for anything ≥ 32 px. |
| `scotri-mark-small.svg` | Optical variant for ≤ 24 px (stroke 6, pupil r 5.2). |
| `scotri-lockup-horizontal.svg` | Mark + wordmark, `currentColor`. |
| `scotri-icon.svg` | 128 px app icon — paper mark on ink, teal pupil. |
| `favicon.svg` | Rounded ink tile, already optically weighted for 16 px. |
| `scotri-monogram.svg` | Secondary avatar: an "S" drawn as a lineage path stepping bronze → silver → gold. For square avatar slots where the lens would be repeated at thumbnail size (e.g. a Jira comment author). |

`scotri-mark.svg`, `scotri-mark-small.svg`, `scotri-lockup-horizontal.svg` and `scotri-monogram.svg`
inherit `currentColor`, so they take the surrounding text colour in both light and dark contexts.

## Geometry

```
viewBox 0 0 48 48
M14.5 16.2 Q 27 7.5 41 24     stroke-width 4, round cap
M33.5 31.8 Q 21 40.5 7 24     stroke-width 4, round cap
circle 24 24 r 3.6            filled
```

The second arc is the first turned 180° about (24, 24). That rotation is what makes the two gaps
identical — so they read as intent rather than as a drawing error — and gives a static eye a slight
sense of scanning.

### Optical scale

The seam and the pupil are the first things lost. Below 32 px thicken the stroke and grow the pupil;
the geometry itself never changes.

| Rendered size | stroke-width | pupil r |
|---|---|---|
| 32 px and up | 4.0 | 3.6 |
| 32 px | 4.6 | 4.1 |
| 24 px | 5.2 | 4.6 |
| 16 px | 6.0 | 5.2 |

## Colour

| Token | Light | Dark | Notes |
|---|---|---|---|
| Ink | `#12151A` | — | 16.9:1 on Paper |
| Paper | `#F6F4EF` | — | |
| Verified (accent) | `#2A6F66` | `#5FB3A6` | 5.4:1 on Paper / 7.5:1 on Ink |
| Caption | `#6B6F76` | `#9BA1A8` | 4.6:1 on Paper — 13 px and up only |

Ink on paper is the whole identity. The accent appears **only where something has been verified** —
a passing assertion, a live link, the pupil on a dark lockup or the app icon. The mark itself is
monochrome everywhere else.

## Type

- **Space Grotesk** (600 / 500) — wordmark, headings, verdicts. Sits at the same stroke weight as the mark.
- **IBM Plex Sans** — body prose: impact analyses, work orders, handoff documents.
- **IBM Plex Mono** — every table name, path and query.

`scotri-lockup-horizontal.svg` keeps the wordmark as live `<text>`, so it needs Space Grotesk in the
rendering context. Inline the SVG in a page that loads the webfont; if it must be used via `<img>`,
in an email, or in a PDF, convert the text to outlines first or it will fall back to a generic sans.

## Motion

Loading states live in [`animations/`](animations/). Each one maps to a real phase of this agent's
lifecycle rather than being a generic spinner — so a user watching the mark knows *which* kind of
waiting is happening.

| State | Class | Use | Timing |
|---|---|---|---|
| Trace | `.scotri-trace` | Boot / first paint. One shot. | 1.5s ease-out, arcs staggered 0.14s |
| Heartbeat | `.scotri-heartbeat` | A presence claim is held. | 2.2s, matches the claim's TTL ping |
| Aperture | `.scotri-aperture` | Reading the mirror or a repo file. | 1.9s ease-in-out |
| Scan | `.scotri-scan` | Working — **the default in-progress state**. | 2.4s linear |
| Queued | `.scotri-queued` | Held at the query governor. | 4s `steps(8)`, dimmed to 0.62 |
| Ring | `.scotri-ring` | Determinate progress (N of M assertions). Set `--scotri-progress` 0→1. | 400ms per step |
| Settle | `.scotri-settle` | Done. Pupil flashes accent, then returns to ink. One shot. | 1.7s ease-out |

**The rule.** The mark is never squashed, bounced, or spun on an arbitrary axis. It only does three
things: turn about its own centre, trim its arcs from their ends, or beat at the pupil. Because the
two arcs are 180°-symmetric, a half turn is a seamless loop — `scan` returns to the exact start pose
with no snap.

**Queued is deliberately not smooth.** It steps. Waiting in a rate-limit queue is not the same as
working, and the motion should not pretend otherwise.

### Files

| File | Use |
|---|---|
| `animations/scotri-motion.css` | All keyframes and state classes. Copy into any web/app UI. |
| `animations/scotri-motion.html` | Markup for every state, and a local preview — open it in a browser. |
| `animations/scotri-scan.svg` | Self-animating, self-contained. For READMEs, docs headers, `<img>` tags. |
| `animations/scotri-aperture.svg` | ” |
| `animations/scotri-heartbeat.svg` | ” |
| `animations/scotri-trace.svg` | ” |

The CSS states inherit `currentColor`. The standalone SVGs cannot (an `<img>` has no inherited
colour), so they carry explicit ink and a `prefers-color-scheme: dark` override built in.

### Reduced motion

Every state is wrapped in `prefers-reduced-motion: reduce` and falls back to the **static mark** —
not to a slower animation. `queued` keeps only its dimmed opacity, since that is information rather
than decoration. If you add a state, add the fallback in the same commit.

## Where the mark is used

| Surface | Static | Motion |
|---|---|---|
| Admin panel (`services/app/static/admin.html`) | `favicon.svg` inlined as the tab icon; the mark and "Scotri Admin" in the header at the 32 px optical weight. Ink/paper/caption tokens throughout; Verified only on `ok` states, live links and the settle flash. | **Trace** once at first paint, then **scan** while any request has been in flight for more than 300 ms. The **Sync now** button's 24 px mark: **ring** while the panel's sync runs (determinate: the run's total percent across its repos), **queued** while another sync holds the shared lock (waiting, not working), **settle** once when a sync started there finishes with no failures. Each repo row's progress bar is ink while it fills, Verified when that repo is done, the error red when it failed. The CSS is copied from `animations/scotri-motion.css`, reduced-motion fallback included. |
| Launcher (`tools/launcher/scotri_launcher.py`) | The favicon tile as the window/dock icon, and the mark at 32 px in the header (paper on a dark window). Tk has no SVG, so both are drawn from the geometry above. | **Trace** at start-up, then **scan** while a Start/Stop/Restart is in flight. Scan always comes to rest on a whole half-turn. Tk can't read the OS reduced-motion setting, so the launcher doesn't honour it. |
| scotri-ui | See its own `docs/brand/README.md`. | |

The admin panel loads Space Grotesk and the Plex faces from Google Fonts, the same link the developer
portal uses. Its CSP (`default-src 'none'`) allows exactly `fonts.googleapis.com` for styles and
`fonts.gstatic.com` for fonts, nothing more; where those hosts are unreachable the stacks fall back to
the system sans and mono.

## Don't

- Close the seam, or make the two gaps unequal.
- Re-weight the strokes independently of the scale table above.
- Outline the pupil, or fill it with anything but the mark colour (the app icon and the inverse
  lockup are the only teal-pupil exceptions).
- Stretch, skew, or add a shadow or gradient.
- Rotate it, except as the `scan` / `queued` / `settle` animations do — about its own centre, in
  whole half-turns.
- Use `scan` where `queued` belongs. If the wait is a rate limit, say so with the stepped state.
- Set the wordmark in anything but Space Grotesk.
- Place it with less than **half the mark's height** of clear space on any side.
