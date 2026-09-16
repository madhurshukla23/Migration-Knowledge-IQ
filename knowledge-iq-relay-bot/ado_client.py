# Copyright (c) Microsoft. All rights reserved.
"""Thin client for the Azure DevOps REST APIs used by the knowledge agent."""

import os

import requests
from requests.auth import HTTPBasicAuth

API_VERSION = "7.1"


class AdoConfigError(RuntimeError):
    """Raised when required Azure DevOps environment variables are missing."""


def _org_url() -> str:
    value = os.environ.get("ADO_ORG_URL")
    if not value:
        raise AdoConfigError("ADO_ORG_URL is not set.")
    return value.rstrip("/")


def _project() -> str:
    value = os.environ.get("ADO_PROJECT")
    if not value:
        raise AdoConfigError("ADO_PROJECT is not set.")
    return value


def _auth() -> HTTPBasicAuth:
    pat = os.environ.get("ADO_PAT")
    if not pat:
        raise AdoConfigError("ADO_PAT is not set.")
    return HTTPBasicAuth("", pat)


def search_work_items(query_text: str, top: int = 10) -> list[dict]:
    """Search work items whose title or description contains the given text."""
    wiql_url = f"{_org_url()}/{_project()}/_apis/wit/wiql?api-version={API_VERSION}"
    safe_query = query_text.replace("'", "''")
    wiql = {
        "query": (
            "SELECT [System.Id], [System.Title], [System.State], [System.WorkItemType] "
            "FROM WorkItems "
            f"WHERE [System.TeamProject] = '{_project()}' "
            # CONTAINS (not CONTAINS WORDS) works without the Work Item Search extension installed.
            f"AND ([System.Title] CONTAINS '{safe_query}' "
            f"OR [System.Description] CONTAINS '{safe_query}') "
            "ORDER BY [System.ChangedDate] DESC"
        )
    }
    response = requests.post(wiql_url, json=wiql, auth=_auth(), timeout=30)
    response.raise_for_status()
    ids = [str(item["id"]) for item in response.json().get("workItems", [])[:top]]
    if not ids:
        return []
    return _get_work_items_by_ids(ids)


def get_work_item(work_item_id: int) -> dict:
    """Get full details for a single work item, including its description."""
    items = _get_work_items_by_ids([str(work_item_id)])
    if not items:
        raise ValueError(f"Work item {work_item_id} was not found.")
    return items[0]


def _get_work_items_by_ids(ids: list[str]) -> list[dict]:
    url = (
        f"{_org_url()}/_apis/wit/workitems"
        f"?ids={','.join(ids)}&$expand=all&api-version={API_VERSION}"
    )
    response = requests.get(url, auth=_auth(), timeout=30)
    response.raise_for_status()
    results = []
    for item in response.json().get("value", []):
        fields = item.get("fields", {})
        results.append(
            {
                "id": item["id"],
                "type": fields.get("System.WorkItemType"),
                "title": fields.get("System.Title"),
                "state": fields.get("System.State"),
                "assigned_to": (fields.get("System.AssignedTo") or {}).get("displayName"),
                "description": fields.get("System.Description"),
                "url": f"{_org_url()}/{_project()}/_workitems/edit/{item['id']}",
            }
        )
    return results


def _default_wiki_identifier() -> str:
    return f"{_project()}.wiki"


def _list_wiki_paths(wiki_id: str) -> list[str]:
    """List every page path in the wiki by walking the page tree."""
    url = (
        f"{_org_url()}/{_project()}/_apis/wiki/wikis/{wiki_id}/pages"
        f"?path=/&recursionLevel=full&api-version={API_VERSION}"
    )
    response = requests.get(url, auth=_auth(), timeout=30)
    response.raise_for_status()
    paths: list[str] = []

    def walk(node: dict) -> None:
        node_path = node.get("path")
        if node_path and node_path != "/":
            paths.append(node_path)
        for child in node.get("subPages") or []:
            walk(child)

    walk(response.json())
    return paths


def search_wiki(query_text: str, top: int = 10) -> list[dict]:
    """Search the project wiki, preferring the Azure DevOps Search extension when available."""
    wiki_id = _default_wiki_identifier()
    search_url = (
        f"https://almsearch.dev.azure.com/{_org_url().rsplit('/', 1)[-1]}"
        f"/{_project()}/_apis/search/wikisearchresults?api-version={API_VERSION}"
    )
    body = {"searchText": query_text, "$top": top}
    response = requests.post(search_url, json=body, auth=_auth(), timeout=30)
    response.raise_for_status()
    payload = response.json()
    results = []
    for result in payload.get("results", []):
        results.append(
            {
                "path": result.get("path"),
                "wiki": (result.get("wiki") or {}).get("name"),
                "project": (result.get("project") or {}).get("name"),
                "hits": result.get("hits"),
            }
        )
    if results:
        return results

    # The Azure DevOps Search extension may not be installed/enabled (infoCode != 0 with no
    # results). Fall back to a client-side scan of page paths and content.
    needle = query_text.lower()
    matches: list[dict] = []
    for path in _list_wiki_paths(wiki_id):
        if needle in path.lower():
            matches.append({"path": path, "wiki": wiki_id, "project": _project(), "hits": None})
            continue
        page = get_wiki_page(path, wiki_id)
        if needle in (page.get("content") or "").lower():
            matches.append({"path": path, "wiki": wiki_id, "project": _project(), "hits": None})
        if len(matches) >= top:
            break
    return matches


def get_wiki_page(path: str, wiki_identifier: str | None = None) -> dict:
    """Get the content of a wiki page at the given path."""
    wiki_id = wiki_identifier or _default_wiki_identifier()
    url = (
        f"{_org_url()}/{_project()}/_apis/wiki/wikis/{wiki_id}/pages"
        f"?path={path}&includeContent=true&api-version={API_VERSION}"
    )
    response = requests.get(url, auth=_auth(), timeout=30)
    response.raise_for_status()
    payload = response.json()
    return {
        "path": payload.get("path"),
        "content": payload.get("content"),
        "url": f"{_org_url()}/{_project()}/_wiki/wikis/{wiki_id}?path={path}",
    }
