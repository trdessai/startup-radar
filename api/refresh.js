// POST /api/refresh — asks GitHub Actions to run the radar workflow now.
//
// The dashboard normally just re-reads the JSON that Actions commits every 30
// minutes, which needs no server at all. This endpoint exists only for the
// "Run new scan" button. It's optional: without the env vars it returns 501 and
// the dashboard tells the user how to enable it.
//
// Vercel env vars (Project -> Settings -> Environment Variables):
//   GITHUB_TOKEN     fine-grained PAT, Actions: read+write on this repo only
//   GITHUB_REPO      "owner/name"
//   GITHUB_WORKFLOW  optional, defaults to radar.yml
//   GITHUB_BRANCH    optional, defaults to main

const API = "https://api.github.com";
const MIN_GAP_SECONDS = 90;   // a scan takes ~1 min; refuse pile-ups

export default async function handler(req, res) {
  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    return res.status(405).json({ error: "Use POST" });
  }

  const token = process.env.GITHUB_TOKEN;
  const repo = process.env.GITHUB_REPO;
  const workflow = process.env.GITHUB_WORKFLOW || "radar.yml";
  const branch = process.env.GITHUB_BRANCH || "main";

  if (!token || !repo) {
    return res.status(501).json({
      error: "Not configured. Set GITHUB_TOKEN and GITHUB_REPO in Vercel.",
    });
  }

  const gh = (path, init = {}) =>
    fetch(`${API}${path}`, {
      ...init,
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        ...(init.headers || {}),
      },
    });

  try {
    // Rate limit without needing any storage: ask GitHub when the last run was.
    // This is what stops a public URL being used to spam workflow dispatches.
    const recent = await gh(
      `/repos/${repo}/actions/workflows/${workflow}/runs?per_page=1`
    );
    if (recent.ok) {
      const { workflow_runs = [] } = await recent.json();
      const last = workflow_runs[0];
      if (last) {
        const age = (Date.now() - new Date(last.created_at)) / 1000;
        if (last.status !== "completed" || age < MIN_GAP_SECONDS) {
          return res.status(429).json({
            error:
              last.status !== "completed"
                ? "A scan is already running"
                : `Last scan was ${Math.round(age)}s ago — wait a moment`,
          });
        }
      }
    }

    const run = await gh(
      `/repos/${repo}/actions/workflows/${workflow}/dispatches`,
      { method: "POST", body: JSON.stringify({ ref: branch }) }
    );

    if (run.status === 204) return res.status(202).json({ ok: true });

    const detail = await run.text();
    if (run.status === 404) {
      return res.status(500).json({
        error: `Not found. Check GITHUB_REPO ("${repo}"), that ${workflow} exists on ${branch}, and that the token has Actions write access.`,
      });
    }
    return res.status(500).json({ error: `GitHub said ${run.status}: ${detail.slice(0, 200)}` });
  } catch (e) {
    return res.status(500).json({ error: e.message });
  }
}
