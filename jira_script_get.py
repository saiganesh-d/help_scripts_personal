# utils/jira_sync.py
"""
Synchronise Jira ticket status for the current software state (CVE pipeline).

Assumptions
-----------
1. `CurrentSwState` model
   - fields: id, run_id, jira_ticket (nullable / char), …

2. `JiraStatus` model
   - fields: id, cve (FK → CurrentSwState), jira_status (char)

3. JIRA Cloud (or on‑prem) REST API v2/3 is reachable.
4. `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_TOKEN` (or password) are exported in env.
"""

import itertools
import logging
import os
from typing import Iterable, List, Sequence

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from requests import HTTPError, RequestException

from myapp.models import CurrentSwState, JiraStatus

log = logging.getLogger(__name__)
BATCH_SIZE = 50  # Jira “IN (…)” clause limit


def _chunks(iterable: Iterable[str], size: int) -> Iterable[List[str]]:
    """Yield fixed‑length batches from *iterable*."""
    it = iter(iterable)
    while chunk := list(itertools.islice(it, size)):
        yield chunk


def _build_jql(keys: Sequence[str]) -> str:
    """Return a safe JQL that fetches a batch of keys."""
    joined = ", ".join(keys)
    return f"key IN ({joined})"


def _jira_session() -> requests.Session:
    """Return a requests Session with Jira auth headers."""
    base_url = os.getenv("JIRA_BASE_URL", "").rstrip("/")
    if not base_url:
        raise RuntimeError("JIRA_BASE_URL env var not set")

    session = requests.Session()
    session.auth = (
        os.getenv("JIRA_EMAIL"),
        os.getenv("JIRA_TOKEN"),
    )
    session.headers.update({"Accept": "application/json"})
    session.base_url = base_url
    return session


def _fetch_status_for(keys: Sequence[str], session: requests.Session) -> dict[str, str]:
    """
    Query Jira search API for *keys* and return {key: status_name}.
    Any key missing in the response will be absent from the dict.
    """
    url = f"{session.base_url}/rest/api/3/search"
    payload = {
        "jql": _build_jql(keys),
        "fields": "status",
        "maxResults": len(keys),
    }

    try:
        r = session.get(url, params=payload, timeout=15)
        r.raise_for_status()
    except HTTPError as exc:
        log.error("Jira HTTP error: %s – payload: %s", exc, payload)
        raise
    except RequestException as exc:
        log.error("Network error talking to Jira: %s", exc)
        raise

    issues = r.json().get("issues", [])
    return {
        issue["key"]: issue["fields"]["status"]["name"]
        for issue in issues
        if issue.get("fields", {}).get("status")
    }


def sync_jira_statuses(run_id: str | int) -> None:
    """
    Update JiraStatus rows for every CurrentSwState with *run_id* that has
    a non‑empty `jira_ticket`.

    Workflow
    --------
    1. Select relevant CurrentSwState rows in a single query.
    2. Batch the ticket keys (max 50 per Jira call) and call Jira once per batch.
    3. Build/update JiraStatus objects using `bulk_update` / `bulk_create`
       within a transaction.
    4. Log and raise on unrecoverable errors; partial failures won’t break others.
    """
    states = (
        CurrentSwState.objects.filter(run_id=run_id)
        .exclude(Q(jira_ticket__isnull=True) | Q(jira_ticket__exact=""))
        .only("id", "jira_ticket")
    )

    if not states.exists():
        log.info("No CurrentSwState rows with Jira tickets for run_id=%s", run_id)
        return

    session = _jira_session()
    to_update = []
    to_create = []

    # 1. Map state.id → ticket
    id_to_ticket = {state.id: state.jira_ticket.strip() for state in states}

    # 2. Jira in batches
    for batch in _chunks(id_to_ticket.values(), BATCH_SIZE):
        try:
            status_map = _fetch_status_for(batch, session)
        except Exception:
            # Decide whether to continue or abort; here we abort for reliability
            raise

        # 3. Prepare ORM objects
        for state_id, ticket in id_to_ticket.items():
            if ticket not in status_map:  # ticket invalid or not returned
                continue

            status_val = status_map[ticket]
            obj, created = JiraStatus.objects.get_or_create(
                cve_id=state_id,  # FK field name
                defaults={"jira_status": status_val},
            )
            if created:
                to_create.append(obj)
            else:
                obj.jira_status = status_val
                to_update.append(obj)

    # 4. Persist in bulk
    with transaction.atomic():
        if to_update:
            JiraStatus.objects.bulk_update(to_update, ["jira_status"])
        # (get_or_create already saved `to_create`, but in other designs you might collect first)
    log.info(
        "Jira sync complete for run_id=%s (created=%d, updated=%d)",
        run_id,
        len(to_create),
        len(to_update),
    )
