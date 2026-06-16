"""Push n8n workflows to n8n Cloud via REST API — no manual UI clicks.

Reads workflow JSON files from `n8n-flows/`, finds existing workflows by name,
creates new ones or updates in place. Reports back the workflow's cloud ID
and dashboard URL so the operator can verify.

Usage:
    python -m core.n8n_deployer --list
    python -m core.n8n_deployer --push n8n-flows/indra__auto_responder.json
    python -m core.n8n_deployer --activate <workflow-id>
    python -m core.n8n_deployer --deactivate <workflow-id>
    python -m core.n8n_deployer --sync   (pushes every JSON in n8n-flows/)

Auth: uses N8N_API_KEY + N8N_API_BASE_URL from .env. Both must be set.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv


N8N_FLOWS_DIR = Path(__file__).resolve().parent.parent / "n8n-flows"

# Fields the n8n REST API accepts on create / update.
_ALLOWED_KEYS_CREATE = {"name", "nodes", "connections", "settings", "staticData"}
_ALLOWED_KEYS_UPDATE = {"name", "nodes", "connections", "settings", "staticData"}

# Secrets read from local .env and injected into workflow static data on push.
# This is the no-Pro-plan alternative to n8n Variables.
# Function nodes / HTTP nodes inside the workflow read these via
# $getWorkflowStaticData('global').<KEY>.
_SECRET_KEYS_FROM_ENV = (
    "META_APP_SECRET",
    "META_VERIFY_TOKEN",
    "META_PAGE_ACCESS_TOKEN",
    "IG_BUSINESS_ACCOUNT_ID",
    "INDRA_N8N_SHARED_SECRET",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
)


def _client() -> httpx.Client:
    base = os.getenv("N8N_API_BASE_URL", "").strip().rstrip("/")
    key = os.getenv("N8N_API_KEY", "").strip()
    if not base or not key:
        print("error: set N8N_API_BASE_URL and N8N_API_KEY in .env", file=sys.stderr)
        sys.exit(2)
    return httpx.Client(
        base_url=base,
        headers={
            "X-N8N-API-KEY": key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        timeout=30.0,
    )


def _cloud_workflow_url(workflow_id: str) -> str:
    """Build a clickable dashboard URL for the workflow.

    n8n Cloud's web UI lives at the same root as the API minus /api/v1.
    """
    base = os.getenv("N8N_API_BASE_URL", "https://app.n8n.cloud/api/v1").rstrip("/")
    web_root = base.replace("/api/v1", "")
    return f"{web_root}/workflow/{workflow_id}"


def list_workflows() -> list[dict]:
    with _client() as c:
        r = c.get("/workflows")
        r.raise_for_status()
        return r.json().get("data", [])


def find_by_name(name: str) -> dict | None:
    for w in list_workflows():
        if w.get("name") == name:
            return w
    return None


def _strip_for_api(payload: dict, allowed: set[str]) -> dict:
    """Filter the workflow JSON down to keys the API accepts."""
    cleaned: dict = {}
    for k in allowed:
        if k in payload:
            cleaned[k] = payload[k]
    cleaned.setdefault("settings", {"executionOrder": "v1"})
    return cleaned


def _collect_secrets_from_env() -> dict[str, str]:
    """Read tracked secret keys from os.environ. Empty if env var missing."""
    out: dict[str, str] = {}
    for k in _SECRET_KEYS_FROM_ENV:
        v = os.getenv(k, "").strip()
        if v:
            out[k] = v
    # Compute the Langfuse Basic Auth header once if both halves are present.
    pk = out.get("LANGFUSE_PUBLIC_KEY", "")
    sk = out.get("LANGFUSE_SECRET_KEY", "")
    if pk and sk:
        token = base64.b64encode(f"{pk}:{sk}".encode()).decode()
        out["LANGFUSE_AUTH_HEADER"] = f"Basic {token}"
    return out


def _merge_static_data(existing: dict | None, new_secrets: dict) -> dict:
    """Merge new secrets into existing staticData.global, preserving other keys
    like postMap, recentPicks, seenNonces. Existing secret values are OVERWRITTEN
    so .env stays authoritative."""
    merged: dict = dict(existing or {})
    g = dict(merged.get("global") or {})
    for k, v in new_secrets.items():
        g[k] = v
    merged["global"] = g
    return merged


def get_workflow(workflow_id: str) -> dict:
    """Fetch a workflow's full body so we can preserve existing static data."""
    with _client() as c:
        r = c.get(f"/workflows/{workflow_id}")
        r.raise_for_status()
        return r.json().get("data", r.json())


def push(filepath: str, with_secrets: bool = True) -> dict:
    """Create or update a workflow from a JSON file.

    If with_secrets=True (default), reads tracked secrets from .env and injects
    them into the workflow's staticData.global, preserving any existing keys
    (like postMap) on the target workflow.

    Returns:
        {"id": "<workflow-id>", "action": "created"|"updated", "url": "...",
         "secrets_pushed": [...], "secrets_missing": [...]}
    """
    p = Path(filepath)
    if not p.exists():
        raise FileNotFoundError(f"Workflow file not found: {filepath}")

    workflow = json.loads(p.read_text())
    name = workflow.get("name")
    if not name:
        raise ValueError("Workflow JSON missing 'name' field")

    secrets: dict[str, str] = _collect_secrets_from_env() if with_secrets else {}
    secrets_missing = [k for k in _SECRET_KEYS_FROM_ENV if k not in secrets]

    existing = find_by_name(name)

    with _client() as c:
        if existing is None:
            body = _strip_for_api(workflow, _ALLOWED_KEYS_CREATE)
            # Merge any staticData from the file with secrets from .env
            body["staticData"] = _merge_static_data(workflow.get("staticData"), secrets)
            r = c.post("/workflows", json=body)
            if r.status_code not in (200, 201):
                raise RuntimeError(f"create failed: HTTP {r.status_code} — {r.text[:400]}")
            data = r.json()
            wid = data.get("id") or data.get("data", {}).get("id")
            return {
                "id": wid, "action": "created", "url": _cloud_workflow_url(wid),
                "name": name, "secrets_pushed": list(secrets.keys()),
                "secrets_missing": secrets_missing,
            }
        else:
            wid = existing["id"]
            body = _strip_for_api(workflow, _ALLOWED_KEYS_UPDATE)
            # Preserve existing static data (postMap, recentPicks, etc.) by
            # fetching the full workflow first.
            full = get_workflow(wid)
            existing_sd = full.get("staticData") or {}
            body["staticData"] = _merge_static_data(existing_sd, secrets)
            r = c.put(f"/workflows/{wid}", json=body)
            if r.status_code not in (200, 201):
                raise RuntimeError(f"update failed: HTTP {r.status_code} — {r.text[:400]}")
            return {
                "id": wid, "action": "updated", "url": _cloud_workflow_url(wid),
                "name": name, "secrets_pushed": list(secrets.keys()),
                "secrets_missing": secrets_missing,
            }


def add_post_to_map(
    workflow_name: str,
    post_id: str,
    niche: str,
    category: str,
    payload: str,
    campaign: str,
    send_dm: bool = True,
) -> dict:
    """Add or update one entry in the workflow's postMap (in static data).

    Lets the operator manage campaigns from the CLI / Indra without touching
    the n8n UI. Preserves all other postMap entries.
    """
    existing = find_by_name(workflow_name)
    if not existing:
        raise RuntimeError(f"Workflow not found: {workflow_name}")
    wid = existing["id"]
    full = get_workflow(wid)
    sd = full.get("staticData") or {}
    g = dict(sd.get("global") or {})
    post_map = dict(g.get("postMap") or {})
    post_map[post_id] = {
        "niche": niche,
        "category": category,
        "payload": payload,
        "campaign": campaign,
        "send_dm": send_dm,
    }
    g["postMap"] = post_map
    # Also refresh secrets from current .env so we don't drift.
    g.update(_collect_secrets_from_env())
    sd["global"] = g

    body = {
        "name": full.get("name"),
        "nodes": full.get("nodes"),
        "connections": full.get("connections"),
        "settings": full.get("settings") or {"executionOrder": "v1"},
        "staticData": sd,
    }
    with _client() as c:
        r = c.put(f"/workflows/{wid}", json=body)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"add_post failed: HTTP {r.status_code} — {r.text[:400]}")
    return {"id": wid, "post_id": post_id, "total_posts": len(post_map)}


def activate(workflow_id: str) -> bool:
    with _client() as c:
        r = c.post(f"/workflows/{workflow_id}/activate")
        return r.status_code in (200, 201)


def deactivate(workflow_id: str) -> bool:
    with _client() as c:
        r = c.post(f"/workflows/{workflow_id}/deactivate")
        return r.status_code in (200, 201)


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Push n8n workflows to n8n Cloud.")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="List all workflows in your n8n Cloud.")
    g.add_argument("--push", metavar="FILE", help="Push a single workflow file (create or update).")
    g.add_argument("--sync", action="store_true", help="Push every .json in n8n-flows/.")
    g.add_argument("--activate", metavar="ID", help="Activate a workflow by ID.")
    g.add_argument("--deactivate", metavar="ID", help="Deactivate a workflow by ID.")
    g.add_argument("--add-post", action="store_true", help="Add a post to a workflow's postMap (requires --workflow, --post-id, --niche, --category, --payload, --campaign).")
    parser.add_argument("--workflow", default="indra · auto-responder (no-AI, template rotation)", help="Workflow name for --add-post (default: auto-responder).")
    parser.add_argument("--post-id", help="IG post ID for --add-post.")
    parser.add_argument("--niche", help="Niche for --add-post.")
    parser.add_argument("--category", default="qualified_request", help="Template category for --add-post.")
    parser.add_argument("--payload", help="Payload URL/text for --add-post (becomes {payload} in DMs).")
    parser.add_argument("--campaign", help="Campaign label for --add-post.")
    parser.add_argument("--no-dm", action="store_true", help="With --add-post: send public reply only, no DM.")
    parser.add_argument("--no-secrets", action="store_true", help="With --push: skip secret injection from .env.")
    args = parser.parse_args()

    if args.list:
        for w in list_workflows():
            tag = "●" if w.get("active") else "○"
            print(f"  {tag}  {w['id']:25s}  {w.get('name', '?')}")
        return 0

    if args.push:
        result = push(args.push, with_secrets=not args.no_secrets)
        marker = "✓ created" if result["action"] == "created" else "↻ updated"
        print(f"{marker}: {result['name']}")
        print(f"  id:  {result['id']}")
        print(f"  url: {result['url']}")
        if result.get("secrets_pushed"):
            print(f"  secrets pushed via static data: {', '.join(result['secrets_pushed'])}")
        if result.get("secrets_missing"):
            print(f"  secrets NOT in .env (workflow will fail until set): {', '.join(result['secrets_missing'])}")
        return 0

    if args.sync:
        files = sorted(N8N_FLOWS_DIR.glob("*.json"))
        if not files:
            print(f"no .json files in {N8N_FLOWS_DIR}", file=sys.stderr)
            return 1
        for f in files:
            try:
                result = push(str(f), with_secrets=not args.no_secrets)
                marker = "✓ created" if result["action"] == "created" else "↻ updated"
                print(f"{marker}: {result['name']}  →  {result['url']}")
            except Exception as exc:
                print(f"✗ failed {f.name}: {exc}", file=sys.stderr)
        return 0

    if args.add_post:
        if not all([args.post_id, args.niche, args.payload, args.campaign]):
            print("error: --add-post requires --post-id, --niche, --payload, --campaign", file=sys.stderr)
            return 2
        result = add_post_to_map(
            workflow_name=args.workflow,
            post_id=args.post_id,
            niche=args.niche,
            category=args.category,
            payload=args.payload,
            campaign=args.campaign,
            send_dm=not args.no_dm,
        )
        print(f"✓ added post {args.post_id} to '{args.workflow}'")
        print(f"  total posts in map: {result['total_posts']}")
        return 0

    if args.activate:
        ok = activate(args.activate)
        print(f"{'✓ activated' if ok else '✗ failed to activate'}: {args.activate}")
        return 0 if ok else 1

    if args.deactivate:
        ok = deactivate(args.deactivate)
        print(f"{'✓ deactivated' if ok else '✗ failed to deactivate'}: {args.deactivate}")
        return 0 if ok else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
