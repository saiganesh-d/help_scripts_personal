import os
import requests

# ── 1.  CONFIG  ────────────────────────────────────────────────────────────────
JIRA_BASE_URL = "https://your‑jira.example.com"      # no trailing slash
JIRA_EMAIL     = os.getenv("JIRA_EMAIL")             # or hard‑code
JIRA_TOKEN     = os.getenv("JIRA_TOKEN")             # API token / password
PAGE_SIZE      = 50                                  # Jira default max is 50

# ── 2.  BUILD REQUEST  ─────────────────────────────────────────────────────────
search_url = f"{JIRA_BASE_URL}/rest/api/3/search"
params = {
    "jql": "ORDER BY created DESC",  # no project clause → whatever you can see
    "fields": "key,summary,status,project",
    "maxResults": PAGE_SIZE,
}
session = requests.Session()
session.auth = (JIRA_EMAIL, JIRA_TOKEN)
session.headers.update({"Accept": "application/json"})

# ── 3.  CALL JIRA  ─────────────────────────────────────────────────────────────
resp = session.get(search_url, params=params, timeout=15)
resp.raise_for_status()                # HTTPError if 4xx/5xx
data = resp.json()

# ── 4.  PRINT RESULTS  ────────────────────────────────────────────────────────
for issue in data.get("issues", []):
    key     = issue["key"]
    proj    = issue["fields"]["project"]["key"]
    summary = issue["fields"]["summary"]
    status  = issue["fields"]["status"]["name"]
    print(f"[{proj}] {key:12}  {status:15}  {summary}")
