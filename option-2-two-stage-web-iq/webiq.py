"""Stage 1: run the search plan against Microsoft Web IQ /web, rate-limited, retried and resumable.

Two ways to reach Web IQ:
  toolbox  A Microsoft Foundry toolbox (MCP endpoint) that wraps a Web IQ project connection. Calls use a
           Microsoft Entra ID token for https://ai.azure.com/.default; the Web IQ key stays in the connection.
  rest     POST https://api.microsoft.ai/v3/search/web, with an API key from WEBIQ_API_KEY (x-apikey header)
           or, with --webiq-auth entra, an app-only Entra token for https://api.microsoft.ai/.default.

Nothing secret is written to disk. The evidence file holds Web IQ content: it's job-scoped working data
that the pipeline deletes when the job finishes. Don't keep it as a cache (see docs/06).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import pathlib
import random
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "shared"))  # the code every option shares

from settings import SETTINGS
from util import write_jsonl

_W = SETTINGS["webiq"]
REST_ENDPOINT = _W["rest_endpoint"]
WEBIQ_SCOPE = _W["scope"]
FOUNDRY_SCOPE = _W["foundry_scope"]
RETRYABLE = {408, 429, 500, 502, 503, 504}
RESULT_FIELDS = ("title", "url", "content", "lastUpdatedAt", "crawledAt", "contentTier")


class RateLimiter:
    """Spaces calls evenly so the job never exceeds `qps` (Web IQ's default AppID limit is 15 QPS, shared by all callers)."""

    def __init__(self, qps: float):
        self.interval = 1.0 / qps
        self.next = 0.0
        self.lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self.lock:
            now = asyncio.get_running_loop().time()
            if self.next > now:
                await asyncio.sleep(self.next - now)
            self.next = max(now, self.next) + self.interval


def _credential(subscription: str | None = None):
    from azure.identity import AzureCliCredential, DefaultAzureCredential

    return AzureCliCredential(subscription=subscription) if subscription else DefaultAzureCredential()


class _TokenCache:
    def __init__(self, scope: str, subscription: str | None = None):
        self.scope, self.cred = scope, _credential(subscription)
        self.token, self.expires = None, 0.0
        self.lock = asyncio.Lock()

    async def get(self) -> str:
        # One fetch at a time: concurrent searches would otherwise each start a slow credential call.
        async with self.lock:
            if not self.token or time.time() > self.expires - 300:
                t = await asyncio.to_thread(self.cred.get_token, self.scope)
                self.token, self.expires = t.token, float(t.expires_on)
            return self.token

    def reset(self) -> None:
        self.token = None


class RestClient:
    """Web IQ REST API: key in the x-apikey header, or an app-only Entra ID token."""

    name = "rest"

    def __init__(self, endpoint: str = REST_ENDPOINT, auth: str = "key", subscription: str | None = None):
        self.endpoint, self.auth = endpoint, auth
        if auth == "key":
            self.key = os.environ.get("WEBIQ_API_KEY")
            if not self.key:
                raise SystemExit("Set WEBIQ_API_KEY for --via rest (or use --webiq-auth entra, or --via toolbox).")
        else:
            self.tokens = _TokenCache(WEBIQ_SCOPE, subscription)

    async def start(self, client) -> None:
        return None

    async def call(self, client, args: dict, req_id: int):
        headers = {"content-type": "application/json"}
        if self.auth == "key":
            headers["x-apikey"] = self.key
        else:
            headers["Authorization"] = f"Bearer {await self.tokens.get()}"
        r = await client.post(self.endpoint, json=args, headers=headers)
        if r.status_code == 200:
            return 200, r.json(), None, None
        if r.status_code == 401 and self.auth != "key":
            self.tokens.reset()
        return r.status_code, None, r.text[:300], r.headers.get("retry-after")


def _rpc_messages(resp) -> list[dict]:
    """JSON-RPC messages from a streamable-HTTP MCP response (plain JSON or server-sent events)."""
    if "text/event-stream" in resp.headers.get("content-type", ""):
        msgs = []
        for block in resp.text.split("\n\n"):
            data = "\n".join(line[5:].strip() for line in block.splitlines() if line.startswith("data:"))
            if data:
                try:
                    msgs.append(json.loads(data))
                except json.JSONDecodeError:
                    pass
        return msgs
    try:
        return [resp.json()]
    except ValueError:
        return []


class ToolboxClient:
    """Web IQ through a Microsoft Foundry toolbox MCP endpoint, authenticated with Microsoft Entra ID."""

    name = "toolbox"

    def __init__(self, url: str, subscription: str | None = None, tool: str | None = None):
        self.url, self.tool = url, tool
        self.tokens = _TokenCache(FOUNDRY_SCOPE, subscription)
        self.session = None

    async def _post(self, client, payload: dict):
        headers = {
            "Authorization": f"Bearer {await self.tokens.get()}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        return await client.post(self.url, headers=headers, json=payload)

    async def start(self, client) -> None:
        init = {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "search-then-batch", "version": "0.1"}},
        }
        r = await self._post(client, init)
        if r.status_code != 200:
            raise SystemExit(f"Toolbox initialize failed: HTTP {r.status_code} {r.text[:300]}")
        self.session = r.headers.get("mcp-session-id")
        await self._post(client, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        if not self.tool:
            r = await self._post(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
            msgs = _rpc_messages(r)
            names = [t["name"] for t in ((msgs[-1].get("result") or {}).get("tools") or [])] if msgs else []
            # The toolbox prefixes tool names with the server label; discover the name instead of assuming a format.
            web = [n for n in names if n == "web" or n.endswith("___web") or n.endswith(".web") or n.endswith("_web")]
            if not web:
                raise SystemExit(f"No Web IQ 'web' tool in the toolbox (tools/list returned {names}). Check the toolbox's project connection.")
            self.tool = web[0]

    async def call(self, client, args: dict, req_id: int):
        r = await self._post(client, {"jsonrpc": "2.0", "id": req_id, "method": "tools/call", "params": {"name": self.tool, "arguments": args}})
        if r.status_code == 401:
            self.tokens.reset()
        if r.status_code != 200:
            return r.status_code, None, r.text[:300], r.headers.get("retry-after")
        msgs = _rpc_messages(r)
        msg = next((m for m in reversed(msgs) if m.get("id") == req_id), msgs[-1] if msgs else {})
        if "error" in msg:
            return 502, None, json.dumps(msg["error"])[:300], None
        res = msg.get("result") or {}
        text = "".join(p.get("text", "") for p in res.get("content") or [] if p.get("type") == "text")
        if res.get("isError"):
            # Web IQ errors such as 429 arrive as tool results with isError set.
            code = 429 if re.search(r"\b429\b|rate.?limit", text, re.I) else 400
            return code, None, text[:300], None
        data = res.get("structuredContent")
        if not isinstance(data, dict):
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                return 502, None, f"unparseable tool result: {text[:200]}", None
        return 200, data, None, None


def make_client(opts):
    if opts.via == "toolbox":
        if not opts.toolbox_url:
            raise SystemExit("--toolbox-url is required with --via toolbox")
        return ToolboxClient(opts.toolbox_url, opts.subscription, opts.toolbox_tool)
    return RestClient(opts.webiq_endpoint, opts.webiq_auth, opts.subscription)


def search_args(entry: dict, opts) -> dict:
    """The /web request for one planned search. Parameters follow the Web IQ Web Search API reference."""
    args = {
        "query": entry["query"],
        "maxResults": opts.max_results,
        "language": opts.language,
        "contentFormat": "passage",
        "maxLength": opts.max_chars,
        "safeSearch": "strict",
    }
    if entry.get("region"):
        args["region"] = entry["region"]
    return args


def normalize_results(data: dict) -> list[dict]:
    # Web IQ omits empty list fields, so a missing webResults means no results.
    return [{k: w.get(k) for k in RESULT_FIELDS if w.get(k) is not None} for w in data.get("webResults") or []]


def _backoff(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            return min(float(_W["max_retry_after_seconds"]), float(retry_after))
        except ValueError:
            pass
    return min(float(_W["backoff_cap_seconds"]), 2**attempt) + random.random()


async def _one(client, api, limiter: RateLimiter, entry: dict, opts, req_id: int) -> dict:
    args = search_args(entry, opts)
    status, err, attempt = None, None, 0
    failed: list[str] = []
    for attempt in range(1, opts.max_attempts + 1):
        await limiter.wait()
        t0 = time.time()
        retry_after = None
        try:
            status, data, err, retry_after = await api.call(client, args, req_id)
        except Exception as ex:  # network errors are retried
            status, data, err = None, None, f"{type(ex).__name__}: {ex}"
        if data is not None:
            entry.update(
                results=normalize_results(data),
                retrieved_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                attempts=attempt,
                failed_attempts=failed,
                via=api.name,
                latency_s=round(time.time() - t0, 2),
            )
            entry.pop("error", None)
            return entry
        failed.append(str(status) if status is not None else "network")
        if status is not None and status not in RETRYABLE and status != 401:
            break
        if attempt < opts.max_attempts:
            await asyncio.sleep(_backoff(attempt, retry_after))
    entry.update(error=f"HTTP {status}: {err}" if status else err, attempts=attempt, failed_attempts=failed)
    return entry


def _read_done(path: pathlib.Path) -> dict[str, dict]:
    """Successful searches already in the evidence file. A last line cut short by an interruption is skipped."""
    done: dict[str, dict] = {}
    if not path.exists():
        return done
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("results") is not None and not e.get("error"):
                done[e["query_id"]] = e
    return done


def _session_stats(entries: list[dict], seconds: float) -> dict:
    ok = [e for e in entries if not e.get("error")]
    failed: dict[str, int] = {}
    for e in entries:
        for s in e.get("failed_attempts") or []:
            failed[s] = failed.get(s, 0) + 1
    return {
        "searches_run": len(entries),
        "searches_succeeded": len(ok),
        "searches_failed": len(entries) - len(ok),
        "attempts": sum(e.get("attempts", 0) for e in entries),
        "failed_attempts_by_status": failed,
        "seconds": round(seconds, 1),
        "mean_latency_s": round(sum(e.get("latency_s", 0) for e in ok) / max(1, len(ok)), 2),
    }


async def _run(plan: dict[str, dict], out_path: pathlib.Path, opts, api=None) -> tuple[list[dict], dict]:
    import httpx

    done = _read_done(out_path)
    todo = [dict(e) for qid, e in plan.items() if qid not in done]
    if len(todo) > opts.max_queries:
        raise SystemExit(f"{len(todo)} searches to run, above --max-queries {opts.max_queries}. Raise it deliberately.")
    print(f"Stage 1: {len(plan)} unique searches planned, {len(done)} already done, {len(todo)} to run via {opts.via}")
    ran: list[dict] = []
    t0 = time.time()
    if todo:
        api = api or make_client(opts)
        limiter, sem = RateLimiter(opts.qps), asyncio.Semaphore(opts.concurrency)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Each search is appended as soon as it finishes, so an interrupted run resumes without repeating (and paying for) finished searches.
        with open(out_path, "a", encoding="utf-8", newline="\n") as log:
            async with httpx.AsyncClient(timeout=float(_W["http_timeout_seconds"])) as client:
                await api.start(client)

                async def bounded(i, e):
                    async with sem:
                        r = await _one(client, api, limiter, e, opts, 100 + i)
                    log.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
                    log.flush()
                    ran.append(r)
                    return r

                for e in await asyncio.gather(*(bounded(i, e) for i, e in enumerate(todo))):
                    if not e.get("error"):
                        done[e["query_id"]] = e
    final = []
    errors = {e["query_id"]: e for e in ran if e.get("error")}
    for qid, e in plan.items():
        merged = dict(done.get(qid) or errors.get(qid) or e)
        merged["used_by"] = e["used_by"]  # the current plan decides which records use each search
        final.append(merged)
    # Replace the file atomically, so an interruption here can't lose the searches already saved.
    tmp = out_path.with_name(out_path.name + ".tmp")
    write_jsonl(tmp, final)
    os.replace(tmp, out_path)
    stats = _session_stats(ran, time.time() - t0)
    stats["already_done"] = len(plan) - len(todo)
    return final, stats


def run_searches(plan: dict[str, dict], out_path: str | os.PathLike, opts, api=None) -> tuple[list[dict], dict]:
    """Run (or resume) the plan and write the evidence file.

    Returns (one entry per planned search, statistics for this session). `api` replaces the Web IQ client (for tests).
    """
    return asyncio.run(_run(plan, pathlib.Path(out_path), opts, api))


def add_search_args(p) -> None:
    p.add_argument("--via", choices=["toolbox", "rest"], default=_W["via"], help="how to reach Web IQ (default: toolbox)")
    p.add_argument("--toolbox-url", help="toolbox consumer endpoint: <project endpoint>/toolboxes/<name>/mcp?api-version=v1")
    p.add_argument("--toolbox-tool", help="tool name in the toolbox (default: discovered with tools/list)")
    p.add_argument("--webiq-endpoint", default=REST_ENDPOINT, help="--via rest: Web IQ /web endpoint, or your gateway in front of it")
    p.add_argument("--webiq-auth", choices=["key", "entra"], default=_W["auth"], help="--via rest: WEBIQ_API_KEY or an app-only Entra ID token")
    p.add_argument("--subscription", help="pin the Azure CLI credential to this subscription")
    p.add_argument("--max-results", type=int, default=int(_W["max_results"]), help="results per search (Web IQ maxResults)")
    p.add_argument("--max-chars", type=int, default=int(_W["max_chars"]), help="characters per result (Web IQ maxLength; also the prompt cap)")
    p.add_argument("--language", default=_W["language"])
    p.add_argument("--qps", type=float, default=float(_W["qps"]), help="stay under your Web IQ AppID limit (15 QPS by default, shared)")
    p.add_argument("--concurrency", type=int, default=int(_W["concurrency"]))
    p.add_argument("--max-attempts", type=int, default=int(_W["max_attempts"]))
    p.add_argument("--max-queries", type=int, default=int(_W["max_queries"]), help="safety cap on searches per run")
