# GSN page monitor

A robot that checks the published Genesis Science Network pages and writes down what
it finds. It never changes those pages. It never logs in anywhere.

Nothing in this repo is secret. That is deliberate — it is public so the checks run
free, including on the Mac runners that are the only place real Safari exists.

---

## The switch

**Right now this runs only when you press the button.** Nothing happens on its own.

To run it once by hand: GitHub → **Actions** tab → **Page monitor** → **Run workflow**.

To turn the hourly robot **ON**: open `.github/workflows/monitor.yml`, find the block
that says `# schedule:`, delete the `#` characters in front of those three lines, and
commit. To turn it **OFF** again, put the `#` back. That is the entire switch — there
is nothing else to remember, and no other place it can be running from.

Start hourly for about a week. Once it has proved itself, change `'0 * * * *'` to
`'0 */3 * * *'` and it drops to every three hours. The comment above those lines lists
the other options.

---

## What it actually checks

For each page, it walks the chain the way a visitor does, and reports **which link in
the chain broke** — not just that something did.

1. **The WordPress page** the public visits — does it answer, is it the right size, is
   the embed tag still there, and does that tag still point at the build it should?
2. **The Vercel build** behind it — does it answer on its own?
3. **Four real browsers** — Chromium and WebKit, at desktop and phone width, loading
   the *real WordPress page*, not the Vercel URL. It looks *inside* the embed: counts
   the links and controls that actually rendered, and checks for a string that only
   exists if the build drew its own content. A build can answer perfectly and still
   show a blank box; this is the check that catches that.
4. **Frame-blocking headers** — see below. This is the highest-value check here.
5. **Links, layout and console** — broken links on our own hosts, content wider than
   the window at phone width, insecure `http://` requests, and console errors.

Special cases it knows about, so it doesn't cry wolf:

- **Graphics Portal answering `401` is healthy.** It's crew-only. A `200` there is the
  alarm — it would mean the lock came off a private tool.
- **The short internship URL** (`/motiongraphics/internship`) is only 1,213 bytes and
  that is complete, not truncated. It's reported, never counted against the page.
- **One bad reading is "Degraded", not "Broken."** It takes two in a row to call a page
  broken. The metadata page flickered twice in one day on 6 August while its Vercel
  build was fine the whole time; that's the reason.

---

## The header check, and why it's the important one

Measured on 6 August 2026: none of the six URLs send `X-Frame-Options` or a
`Content-Security-Policy` that restricts framing, and there's no Cloudflare in front.
**That absence is why the embeds work at all.** Every public GSN page is a WordPress
page with a Vercel build inside it, so the day one of those headers appears, every page
goes blank while the Vercel URLs keep looking perfect on their own — which is the exact
symptom that is hardest to diagnose by eye.

The direction matters, and the original brief for this project had it backwards. Whether
page B may be shown inside page A is decided by **B's** headers:

| What appears | Where | Effect | Verdict |
|---|---|---|---|
| `X-Frame-Options`, CSP `frame-ancestors` | on the **Vercel** build | it refuses to be embedded → page goes blank | **alarm** |
| CSP `frame-src`, `default-src` | on the **WordPress** page | it refuses to load its child → page goes blank | **alarm** |
| `X-Frame-Options`, CSP `frame-ancestors` | on the **WordPress** page | only stops GSN being framed *by others* | noted, no alarm |

Both alarming directions are checked every run. `python3 test_headers.py` proves the
logic in both directions, and the workflow runs it before it trusts any verdict.

---

## What this cannot see — read this before trusting a green tick

Three honest limits. A board that hides these is worse than no board.

1. **It is always signed out, so it cannot see the logged-in WordPress fatal.** The
   "critical error" you hit on 6 August fires only on a logged-in render. Anonymous
   fetches came back clean every time. Green here answers *"does a visitor see the
   page?"* — it does not answer *"does it work when you're logged in?"* Only the
   WordPress fatal-error email names that file and line.
2. **Hourly sampling misses short flickers.** A 90-second wobble between two checks
   is invisible unless it happens to land on the hour. What it catches reliably is the
   *persistent* failure — a header appears, a build stops answering, an embed empties —
   and that is the one worth catching.
3. **Playwright's WebKit is not real Safari.** It's close, but real Safari partitions
   `localStorage` and blocks third-party cookies inside cross-origin iframes, which is
   precisely how every one of these pages is built, and the stations build already uses
   `localStorage`. So the daily Mac run is a separate job, and until it runs the Safari
   rows say which engine produced them. They are never labelled a Safari pass when they
   aren't one. Note the Mac job writes its result to a file that the **next** hourly
   sweep folds in — so a real-Safari pass shows up on the board within the hour, not
   the instant it finishes. That decoupling is deliberate: it stops the two jobs racing
   each other. A Safari result older than 26 hours is ignored and the rows revert to
   saying WebKit.

Two targets deserve a footnote on how thoroughly the *inside* of their embed is checked.
**GSN Homepage** is a video player: it renders no links at all, so it's floored on
control count and the word `LIVE` rather than links. **Episode Metadata** sits behind an
access-code gate, but the records render behind it, so it's floored at 50 in-frame links
against the 122 measured. Every other target is floored on links plus a content string.

---

## Why nothing auto-fixes yet

The ask was for agents that fix problems themselves. Checked, and the honest answer is
that the surface an agent could safely touch is currently **zero**:

| Page | Where its source lives | Can this workflow fix it? |
|---|---|---|
| Stations | `~/Desktop/gsn-stations-deploy` | No — not a git repo, and it's on your Mac |
| Episode Metadata | `gsr-broll-sourcing/metadata-portal/vercel-deploy` | No — private repo this workflow can't reach |
| Internship | `GSR-Internship-Program/landing` | No — same |
| Graphics Portal | `gsr-broll-sourcing/graphics-portal` | No — same |
| GSN Homepage | WordPress, no repo at all | No — and it must never try |

On top of that, most real failures in this chain are WordPress, Elementor, plugin or
host problems, where an agent has no safe write path at any price. So `fix_attempt` is
always `null` and the code says why.

What it produces instead is the thing that's actually useful: a **paste-ready handoff
prompt** on every non-green page. Self-contained — the URL, the layer that broke, the
numbers, what's already been ruled out, and where the source lives — so you can drop it
cold into a fresh AI session and get straight to the fix. That's the real deliverable.

If a fix is ever attempted, the rule is fixed: it must be a small reversible change in a
repo this workflow owns, and the **full four-way scan must re-run afterwards to prove
it**. Never WordPress, never Elementor, never a plugin, never DNS, and never a deploy
without your say-so.

---

## Where the reports go

```
data/latest.json              the current state of all five pages + a 7-day strip
data/history/<page>/*.json    one file per page per run, pruned at 7 days
data/_http.json               working file: the two HTTP layers
data/_browsers.json           working file: the four browser combinations
data/_safari.json             working file: the daily real-Safari pass
```

The shape is fixed by `CONTRACT.md` in the private `portal-hub` repo, under
`page-monitor/`. The dashboard reads `latest.json` and nothing else. Don't rename a key
without bumping `schema_version` there first — the board is the only consumer and it
will silently render wrong rather than complain.

Seven days of history is kept on purpose. *"It breaks at 3am every night"* is not a
clue, it's the diagnosis.

---

## Running it yourself

```bash
npm ci
npx playwright install chromium webkit
npm run check          # http -> browsers -> rollup
```

Individually: `npm run http`, `npm run browsers`, `npm run merge`.
`python3 check.py --stage merge --dry-run` prints the report without writing anything.

---

## Rules this repo keeps

- No credentials, tokens, cookies or passcodes — not in code, commits, logs or reports.
  There are none to leak: every check is anonymous, and the only permission the workflow
  holds is writing its own reports back to this repo.
- No screenshots. A screenshot can capture more than intended, and the reports are
  public. Byte counts and booleans carry the same information.
- No copy of the sites is kept here. Your words: *"i dont need to have my own version of
  the site. especially if that keeps me from seeing if something is broken on the actual
  site."*
- The monitored pages are never modified.
- If `targets.json` and reality disagree — an embed now points somewhere new — that is
  **reported as a fault**, never quietly corrected. Correcting it would erase the alarm.
