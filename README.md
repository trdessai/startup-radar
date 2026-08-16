# Indian Startup Radar — free edition

Watches Indian startup news every 30 minutes, scores it, and shows launches, funding
rounds and product news on a web dashboard — with optional Slack alerts.
**Total running cost: ₹0.** No LLM, no paid APIs, no server.

```
🚀 LAUNCH  ·  Pre-Seed  ·  Rs16 crore  ·  100
Kaya
Stealth startup Kaya emerges out of stealth with Rs 16 crore pre-seed
Founders:  Ravi Kumar — find on X · LinkedIn  |  Meera Iyer — find on X · LinkedIn
[ Read source ]  [ Background ]
Entrackr · 4m ago · launch in headline, Rs16 crore, Pre-Seed, stealth exit
```

Everything in the stack is free: RSS is free, Google News RSS is free, Slack incoming
webhooks are free, and **GitHub Actions is unlimited on public repositories**.

## Push this to GitHub

From inside this folder, in your terminal. Create the repo first at github.com/new —
name it `startup-radar` and set it to **Public** (that's what makes Actions free and
unlimited), and don't tick "Add a README", since this folder already has one.

```bash
git init
git add .
git commit -m "Indian startup radar"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/startup-radar.git
git push -u origin main
```

Then check that `.github/workflows/radar.yml` appears in the repo on GitHub. If it
doesn't, `git add .` missed it — run `git add -f .github/workflows/radar.yml` and push
again. Without that file nothing is ever scheduled.

Next: Actions tab → enable workflows → **Run workflow**. Then deploy the dashboard
(see [Dashboard on Vercel](#dashboard-on-vercel) below).

Slack is optional and off by default. The scan runs dashboard-only unless you add a
`SLACK_WEBHOOK_URL` secret.

---

## Setup (about 10 minutes)

**1. Fork or create the repo — make it public.**
This matters. Public repos get unlimited free Actions minutes. Private repos on the
Free plan get 2,000 minutes/month, and 48 runs a day would burn through that in about
three weeks. Nothing secret lives in the repo; the webhook is a GitHub secret.

**2. Slack — optional.** Skip this entirely if you only want the dashboard; leave the
secret unset and the scan runs dashboard-only. To use it:
api.slack.com/apps → Create New App → From scratch → pick your workspace →
**Incoming Webhooks** → toggle on → **Add New Webhook to Workspace** → pick a channel
→ copy the `https://hooks.slack.com/services/…` URL.

**3. Add it as a repo secret (only if you did step 2).**
Repo → Settings → Secrets and variables → Actions → New repository secret.
Name it `SLACK_WEBHOOK_URL`, paste the URL.

**4. Test locally before going live.**

```bash
pip install -r requirements.txt
python check_feeds.py              # are the feed URLs alive?
python radar.py --dry-run          # prints what it would post, sends nothing
python radar.py --dry-run --show-all   # everything, including what got filtered out
```

**5. Turn it on.** Repo → Actions tab → enable workflows. It runs every 30 minutes.
You can also hit **Run workflow** to trigger one by hand.

## About X

**Your blue tick doesn't include API access** — they're separate products. In February
2026 X replaced its tiered API pricing with pay-per-use and closed Basic and Pro to new
signups. There is no free tier: reads are billed per object at roughly $0.005 each. So
this build doesn't touch the X API at all.

In practice you lose less than you'd think. Founders who launch something also post
about it in places that *are* free — and Indian startup media covers X announcements
within hours, which the RSS feeds pick up.

If you do find a working X-to-RSS bridge, drop the URL into the `feeds:` list in
`config.yaml`. No code change needed. Be warned that these bridges break often.

## What it does instead of an LLM

Scoring is rules, not a model. Roughly:

| | |
|---|---|
| launch verb in the **headline** | +33 |
| funding verb in the headline | +30 |
| amount found ($4.5M, ₹16 crore) | +6 to +18, scaled |
| seed / pre-seed | +12 (early rounds are the ones nobody's covered) |
| out of stealth | +12 |
| named founder | +5 |
| feed weight (per-source trust, in config) | −5 to +8 |
| listicle, opinion, explainer, roundup | **−45** |
| markets/stock story | **−35** |
| people move (appoints, hires, resigns) | −12 |
| question-mark headline | −12 |
| no India signal at all | score forced to 0 |

Then dedupe drops anything already alerted in the last 5 days, and the top 10 above
`min_score` get posted.

It's blunter than a model, but the sources do most of the work: Entrackr, Inc42 and
VCCircle publish almost nothing *except* Indian startup news, so the job is mostly
"is this an event or a think-piece", which rules handle fine.

## Founder info — free version

The agent pulls **founder names** out of the article text (`founded by X and Y`,
`co-founder Z said`), which costs nothing and works most of the time.

For handles it deliberately does not guess:

- If the article printed an `@handle` near the founder's name, you get a direct link.
- Otherwise you get **one-click search buttons** — X people-search and a LinkedIn
  Google search, both pre-filled with the name and company.

That second case is one extra click, and it's the honest trade. Automated
name-to-handle matching without a paid search API is a coin flip, and pitching the
wrong person is the kind of mistake founders remember.


## Dashboard on Vercel

A web dashboard of the same alerts, free to host. **Vercel serves the UI; GitHub
Actions stays the scheduler.** That split is not a preference — Vercel's Hobby plan
caps cron at *once per day*, and a `*/30 * * * *` expression fails deployment outright
with "Hobby accounts are limited to daily cron jobs". So the 30-minute loop stays where
it already works, and the dashboard just reads what it produces.

```
GitHub Actions (every 30 min)
   radar.py  ->  Slack alert
             ->  public/data.json  ->  committed to the repo
                                          |
                    Vercel dashboard reads it straight from GitHub
```

Because the page reads `data.json` from raw.githubusercontent.com, a new scan appears
without redeploying anything. `vercel.json` has an `ignoreCommand` that skips rebuilds
on data-only commits, so 48 commits a day cost zero deployments.

### Deploy (about 5 minutes)

1. **Edit one line.** In `public/index.html`, set `REPO` to your `owner/repo`.
2. vercel.com → **Add New Project** → import the repo → **Deploy**. No settings to
   change; `vercel.json` points the output at `public/`.
3. Done. The dashboard is live and updates itself every 30 minutes.

### The two refresh buttons

**Refresh** re-reads the latest results. Free, instant, no server involved — this is
the one you'll use. The page also re-reads quietly every 5 minutes on its own.

**Run new scan** actually goes and fetches the sources right now. This needs a small
serverless function (`api/refresh.js`) because triggering GitHub Actions requires a
token that can't sit in client-side code. It's optional — without the env vars the
button explains what to add. To turn it on:

- GitHub → Settings → Developer settings → **Fine-grained personal access token**,
  scoped to this one repo, permission **Actions: read and write**.
- Vercel → Project → Settings → Environment Variables:
  `GITHUB_TOKEN` (the token) and `GITHUB_REPO` (`owner/repo`). Redeploy.

The endpoint refuses a dispatch if a scan is already running or one finished under 90
seconds ago, checked against the GitHub API. That's the rate limit — the URL is public,
so it needs one, and doing it statelessly avoids needing a database.

### What's on it

Stories are ranked by score, with a rail on the left showing where each one landed.
Filter by category, search, or click any **reason chip** ("launch in headline",
"Pre-Seed", "₹16 crore") to see every story that scored for the same signal — which is
also the fastest way to work out whether a rule is pulling its weight.

Switch on **near-misses** to include stories scoring within 20 of the threshold. That's
the view for tuning `min_score`: if good stories keep showing up as near-misses, your
threshold is too high.

**Mark covered** greys out a story you've written up. Stored in your browser only, so
it doesn't sync across devices and clearing site data resets it.

Keyboard: `/` focuses search, `R` refreshes.

### One caveat

Vercel's Hobby plan is for non-commercial use. A personal newsroom tool is fine; if this
becomes part of a paid publication's workflow, that's Pro territory. The GitHub Actions
half stays free either way, and Slack alerts don't depend on the dashboard at all.

## Tuning

Everything is in `config.yaml`. No code changes.

**Too noisy** → raise `min_score` to 65, or lower `max_alerts_per_run`.
Each alert prints its scoring reasons in the Slack context line, so you can see exactly
what pushed something over.

**Missing stories** → `python radar.py --dry-run --show-all --lookback 720` shows a
full day with nothing filtered, and the score for each. Find the story you wanted, see
what it scored, set `min_score` just below it.

**Trust a source more or less** → change its `weight` (added to every story from that
feed). Entrackr is at +8 by default; set a noisy feed to `-5`.

**Add feeds** → add to the `feeds:` list, set `india_native: true` if the outlet only
covers Indian startups, then run `python check_feeds.py`.

**Kill a recurring annoyance** → add the phrase to `NOISE_RE` in `radar.py`.

## Known limits

- **Google News links go through a redirect page.** The alert works, the link just
  costs one extra hop.
- **Rules miss nuance.** A cleverly-worded launch with no launch verb in the headline
  gets a low score. Run `--show-all` weekly for a while to catch what's slipping.
- **Actions cron drifts.** Scheduled runs often fire a few minutes late under load.
  `lookback_minutes: 90` is deliberately triple the interval so a late or skipped run
  catches up rather than losing stories.
- **Company extraction is a heuristic.** It reads the words before the verb. Odd
  headline structures produce odd names; the headline is always shown underneath.

## How state works

`state/seen.json` is the "already alerted" memory, and the workflow commits it back to
the repo after each run. Two reasons this beats `actions/cache`: cache entries get
evicted, and every eviction means re-alerting everything you already saw. And because
public-repo schedules are **disabled after 60 days of no repository activity**, those
commits double as the keepalive that keeps the cron running indefinitely.

The file self-prunes to 5 days and stays a few KB.

## Files

```
radar.py            the whole agent — fetch, dedupe, score, post
config.yaml         feeds, Google News queries, thresholds
selftest.py         50 offline tests, no network or keys needed
check_feeds.py      are the feed URLs still alive
state/seen.json     dedupe memory, committed back each run
public/index.html   the dashboard — one file, no build step
public/data.json    7 days of scored stories, read by the dashboard
api/refresh.js      optional: the "Run new scan" button
vercel.json         static output + skip rebuilds on data commits
.github/workflows/radar.yml
```

## Tests

```bash
python selftest.py
```

Fifty checks, no network, no keys. Most of them are score calibration and dedupe
fixtures, because with no model in the loop the rules *are* the product — these are
what stop a config tweak silently breaking the feed. Two examples:

- *"Zepto raises $340M Series F"* and *"Zepto closes $340M in Series F round"* must
  **merge** — same story, two outlets.
- *"Zepto raises $60M Series D"* and *"Zepto raises $340M Series F"* must **not** —
  same company, different news. Merging those would silently bury the newer round.

Thresholds throughout lean toward showing two stories rather than one. A duplicate
alert costs you ten seconds; a suppressed story costs you the scoop.

## If you later want the smarter version

The main upgrade path is swapping rule-based scoring for a model call, which is what
makes it good at borderline cases and lets you steer the beat in plain English
("prioritise pre-Series A, ignore edtech"). On Claude Haiku that's a few dollars a
month at this volume. Everything else — sources, dedupe, Slack format — stays as is.
