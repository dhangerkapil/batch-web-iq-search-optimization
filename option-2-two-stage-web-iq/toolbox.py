"""Create, test and delete the Microsoft Foundry toolbox that stage 1 uses to call Web IQ.

The toolbox wraps an existing Web IQ project connection and exposes it as an MCP endpoint that accepts
Microsoft Entra ID tokens (scope https://ai.azure.com/.default), so pipeline code never handles the
Web IQ key. Callers need the Foundry User role on the project.

Examples, from this folder:
  python toolbox.py create --project-endpoint https://<resource>.services.ai.azure.com/api/projects/<project> \\
      --name webiq-search --connection <Web IQ connection name>
  python toolbox.py test --project-endpoint https://<resource>.services.ai.azure.com/api/projects/<project> --name webiq-search
  python toolbox.py delete --project-endpoint https://<resource>.services.ai.azure.com/api/projects/<project> --name webiq-search
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "shared"))  # the code every option shares

from settings import SETTINGS
from webiq import FOUNDRY_SCOPE, ToolboxClient

_T = SETTINGS["toolbox"]
API = f"api-version={_T['api_version']}"
HTTP_TIMEOUT = float(_T["http_timeout_seconds"])
CREATE_TIMEOUT = float(_T["create_timeout_seconds"])


def _headers(subscription: str | None) -> dict:
    from azure.identity import AzureCliCredential, DefaultAzureCredential

    cred = AzureCliCredential(subscription=subscription) if subscription else DefaultAzureCredential()
    return {"Authorization": f"Bearer {cred.get_token(FOUNDRY_SCOPE).token}", "Content-Type": "application/json"}


def consumer_endpoint(project_endpoint: str, name: str) -> str:
    return f"{project_endpoint.rstrip('/')}/toolboxes/{name}/mcp?{API}"


def cmd_create(a) -> None:
    import httpx

    pe, h = a.project_endpoint.rstrip("/"), _headers(a.subscription)
    existing = httpx.get(f"{pe}/toolboxes/{a.name}?{API}", headers=h, timeout=HTTP_TIMEOUT)
    if existing.status_code == 200 and not a.new_version:
        raise SystemExit(f"Toolbox '{a.name}' already exists. Use --new-version to add a version, or choose another name.")
    body = {
        "description": a.description,
        "tools": [
            {
                "type": "mcp",
                "server_label": a.server_label,
                "server_url": a.server_url,
                "require_approval": "never",
                "project_connection_id": a.connection,
                "allowed_tools": ["web"],
                "description": "Microsoft Web IQ web search",
            }
        ],
    }
    r = httpx.post(f"{pe}/toolboxes/{a.name}/versions?{API}", headers=h, json=body, timeout=CREATE_TIMEOUT)
    if r.status_code not in (200, 201):
        raise SystemExit(f"Create failed: HTTP {r.status_code} {r.text[:400]}")
    print(f"Created toolbox '{a.name}' version {r.json().get('version')} around connection '{a.connection}'.")
    print(f"Consumer endpoint (use as --toolbox-url): {consumer_endpoint(pe, a.name)}")
    if existing.status_code == 200:
        print("A new version doesn't become the default automatically; promote it before relying on the consumer endpoint.")


async def _test(url: str, subscription: str | None, query: str, region: str) -> None:
    import httpx

    t = ToolboxClient(url, subscription)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        await t.start(client)
        t0 = time.time()
        status, data, err, _ = await t.call(
            client, {"query": query, "region": region, "maxResults": 3, "contentFormat": "passage", "maxLength": 300, "safeSearch": "strict"}, 7
        )
    if data is None:
        raise SystemExit(f"Tool '{t.tool}' failed: HTTP {status} {err}")
    results = data.get("webResults") or []
    print(f"Tool '{t.tool}' returned {len(results)} results in {time.time() - t0:.1f}s (one billable Web IQ call):")
    for w in results:
        print(f"  - {(w.get('title') or '')[:80]} | {w.get('url')}")


def cmd_test(a) -> None:
    asyncio.run(_test(consumer_endpoint(a.project_endpoint, a.name), a.subscription, a.query, a.region))


def cmd_delete(a) -> None:
    import httpx

    pe, h = a.project_endpoint.rstrip("/"), _headers(a.subscription)
    r = httpx.get(f"{pe}/toolboxes/{a.name}/versions?{API}", headers=h, timeout=HTTP_TIMEOUT)
    if r.status_code == 404:
        print(f"Toolbox '{a.name}' doesn't exist.")
        return
    r.raise_for_status()
    versions = [v.get("version") for v in (r.json().get("data") or r.json().get("value") or [])]
    for v in versions:
        d = httpx.delete(f"{pe}/toolboxes/{a.name}/versions/{v}?{API}", headers=h, timeout=HTTP_TIMEOUT)
        print(f"delete version {v}: HTTP {d.status_code}")
    g = httpx.get(f"{pe}/toolboxes/{a.name}?{API}", headers=h, timeout=HTTP_TIMEOUT)
    if g.status_code != 404:
        d = httpx.delete(f"{pe}/toolboxes/{a.name}?{API}", headers=h, timeout=HTTP_TIMEOUT)
        print(f"delete toolbox: HTTP {d.status_code}")
        g = httpx.get(f"{pe}/toolboxes/{a.name}?{API}", headers=h, timeout=HTTP_TIMEOUT)
    print(f"Check: GET toolbox -> HTTP {g.status_code} ({'deleted' if g.status_code == 404 else 'still present'})")


def main() -> None:
    ap = argparse.ArgumentParser(prog="python toolbox.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    tsub = ap.add_subparsers(dest="toolbox_cmd", required=True)
    for name, fn in (("create", cmd_create), ("test", cmd_test), ("delete", cmd_delete)):
        q = tsub.add_parser(name)
        q.add_argument("--project-endpoint", required=True, help="https://<resource>.services.ai.azure.com/api/projects/<project>")
        q.add_argument("--name", required=True, help="toolbox name")
        q.add_argument("--subscription", help="pin the Azure CLI credential to this subscription")
        q.set_defaults(fn=fn)
        if name == "create":
            q.add_argument("--connection", required=True, help="name of the Web IQ project connection")
            q.add_argument("--server-label", default="webiq")
            q.add_argument("--server-url", default=_T["mcp_server_url"])
            q.add_argument("--description", default="Microsoft Web IQ web search for the search stage of a batch job")
            q.add_argument("--new-version", action="store_true")
        if name == "test":
            q.add_argument("--query", default="Microsoft Web IQ")
            q.add_argument("--region", default="US")
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
