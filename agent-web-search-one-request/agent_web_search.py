r"""One request per line, with the web_search tool: each line goes to the Responses API as one request, directly or
through a Microsoft Foundry prompt agent, and the run shows how many web searches ran inside each request.

  plan    the request run would send for a line (with --mode agent, the agent's definition and a call); calls nothing
  run     send each line once and measure it:
            --mode request   the Responses API of a model deployment, as a batch line's body
            --mode agent     a prompt agent with the web_search tool attached directly (no toolbox), created for the run
                             and deleted after it; the model, instructions and tool sit in the agent
  report  a run's table again, and its summary.md

The input is records, built into option 1's lines (../option-1-bounded-web-search), or --lines, a Batch input file you
already have. Bodies are sent as they are, apart from the changes you ask for, which are printed, and store: false. In
agent mode a line's model, instructions, tools and other definition settings go into the agent and the rest of the line
into each call; a line setting that neither takes is listed with the changes and not sent.

Examples, from this folder:
  python agent_web_search.py plan ../samples/brand-owner/records.jsonl --first 2
  python agent_web_search.py run ../samples/brand-owner/records.jsonl --workdir runs/request --mode request \
      --base-url https://<resource>.openai.azure.com/openai/v1/ --deployment <Global Standard deployment>
  python agent_web_search.py run ../samples/brand-owner/records.jsonl --workdir runs/agent --mode agent \
      --project-endpoint https://<resource>.services.ai.azure.com/api/projects/<project> --deployment <model deployment>
  python agent_web_search.py run --lines my-batch-input.jsonl --first 5 --workdir runs/mine --mode request \
      --base-url https://<resource>.openai.azure.com/openai/v1/ --deployment <Global Standard deployment>
  python agent_web_search.py report runs/request
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import json
import pathlib
import secrets
import signal
import sys
import threading
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "shared"))  # the code every option shares

import cli
import exits
import one_request as one
import prompt_agent
from settings import SETTINGS
from util import now, read_json, read_jsonl, write_json, write_jsonl

_D, _S = SETTINGS["defaults"], SETTINGS["agent_sample"]
DESIGNS = ("web-search", "bounded-web-search")
METRICS, RESPONSES, RUN, SUMMARY = "metrics.jsonl", "responses.jsonl", "run.json", "summary.md"


def load_lines(a) -> list[dict]:
    """The lines to send: built from records with option 1's code, or read from a Batch input file; the first --first."""
    if bool(a.records) == bool(a.lines):
        raise SystemExit("Give records (built into option 1's lines) or --lines <Batch input file>, one of the two.")
    if a.lines:
        lines = read_jsonl(a.lines)
        bad = [str(x.get("custom_id")) for x in lines if x.get("url") != "/v1/responses" or not isinstance(x.get("body"), dict)]
        if bad:
            raise SystemExit(f"--lines takes Responses API batch lines (url /v1/responses, with a body); these aren't: {bad[:5]}")
    else:
        import pipeline

        workload, records = pipeline.load_records(a)
        opts = SimpleNamespace(
            deployment=a.deployment or _D["deployment"],
            max_tool_calls=int(_D["max_tool_calls"]) if a.max_tool_calls is None else a.max_tool_calls,
            reasoning_effort=a.reasoning_effort or _D["reasoning_effort"],
            max_output_tokens=int(_D["max_output_tokens"]) if a.max_output_tokens is None else a.max_output_tokens,
        )
        lines = pipeline.build_lines(workload, records, a.design, opts)
    ids = [x.get("custom_id") for x in lines]
    if len(set(ids)) != len(ids):
        raise SystemExit("The lines' custom_id values aren't unique.")
    return lines[: a.first] if a.first else lines


def requests_for(a, lines: list[dict]) -> tuple[list[tuple[str, dict]], list[str]]:
    """Each line's request, and every change made to the lines. Lines built from records are option 1's as they are; a
    --reasoning-effort on web-search lines, which option 1 leaves without one, is a change."""
    built = bool(a.records)
    out: list[tuple[str, dict]] = []
    changes: list[str] = []
    for line in lines:
        body, changed = one.apply_overrides(
            line["body"],
            deployment=None if built else a.deployment,
            tool=a.tool,
            max_tool_calls=None if built else a.max_tool_calls,
            reasoning_effort=a.reasoning_effort if (not built or a.design == "web-search") else None,
            max_output_tokens=None if built else a.max_output_tokens,
            service_tier=a.service_tier,
            count_sources=a.count_sources,
        )
        out.append((str(line["custom_id"]), body))
        changes += [c for c in changed if c not in changes]
    return out, changes


def _changes(changes: list[str]) -> None:
    if changes:
        print("Changes made to the lines before sending them:")
        for c in changes:
            print(f"  - {c}")
    else:
        print("The lines are sent exactly as built: no changes.")


def _dump(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


def agent_calls(reqs: list[tuple[str, dict]]) -> tuple[list[tuple[str, dict, list, dict]], list[str]]:
    """Each line's agent definition, input and call settings, and the line settings agent mode has no place for."""
    out: list[tuple[str, dict, list, dict]] = []
    dropped: list[str] = []
    for cid, body in reqs:
        definition, given, call, left = prompt_agent.split_body(body)
        out.append((cid, definition, given, call))
        dropped += [k for k in left if k not in dropped]
    return out, dropped


def _not_sent(dropped: list[str]) -> list[str]:
    return [f"not sent in agent mode, which has no place for them: {', '.join(dropped)}"] if dropped else []


def cmd_plan(a) -> int:
    lines = load_lines(a)
    reqs, changes = requests_for(a, lines)
    if a.mode == "agent":
        calls, dropped = agent_calls(reqs)
        changes += _not_sent(dropped)
    print(f"{len(reqs)} line(s). run sends each one once, as one HTTPS request; plan sends nothing.")
    _changes(changes)
    cid, body = reqs[0]
    if a.mode == "agent":
        definitions = {prompt_agent.definition_key(d) for _, d, _, _ in calls}
        _, definition, given, call = calls[0]
        print(f"\nAgent mode: run creates {len(definitions)} version(s) of one prompt agent, with the web_search tool attached "
              f"directly (no toolbox), and deletes the agent after the run. The definition for {cid}:")
        print("POST <project endpoint>/agents/<agent name>/versions?api-version=" + str(_S["api_version"]))
        print(_dump({"definition": definition}))
        print(f"\nThen one call per line. For {cid}: POST <project endpoint>/openai/v1/responses")
        print(_dump({**prompt_agent.reference("<agent name>", "<version>"), "input": given, **call}))
    else:
        print(f"\nThe request for {cid}: POST <base url>responses, for example https://<resource>.openai.azure.com/openai/v1/responses")
        print(_dump(body))
    print(f"\nThe model decides how many web searches to run inside each request; the response comes back once, with a "
          f"web_search_call item for each search and the answer. run sends {len(reqs)} request(s) like this one.")
    return 0


def _agent_name() -> str:
    """A name no other run picks: the UTC time, and a random suffix for runs started in the same second."""
    return f"{_S['agent_name_prefix']}-{dt.datetime.now(dt.timezone.utc):%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def _append(path: pathlib.Path, row: dict) -> None:
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def _record(wd: pathlib.Path, a, cid: str, outcome: dict, rows: dict) -> None:
    """Keep the whole response locally (responses.jsonl, which holds search content: don't share it) and the shareable
    numbers in metrics.jsonl, and print one line."""
    _append(wd / RESPONSES, {"custom_id": cid, **{k: outcome.get(k) for k in ("http_status", "http_requests", "seconds", "held",
                                                                               "error_kind", "error", "response")}})
    row = one.measure(cid, a.mode, outcome)
    _append(wd / METRICS, row)
    rows[cid] = row
    if row["status"] is None:
        print(f"  {cid}: {'held' if row['held'] else 'failed'} ({row['error_kind']}) after {row['http_requests']} HTTP request(s)", flush=True)
    else:
        billed = "not reported" if row["web_search_requests"] is None else row["web_search_requests"]
        print(f"  {cid}: {row['status']} in {row['seconds']} s, {row['http_requests']} HTTP request(s), {row['web_search_calls']} "
              f"web_search_call item(s) {one._cell(row['web_search_actions'])}, billed search requests {billed}", flush=True)


class _CtrlC:
    """Ctrl+C during a run. The first one stops it: send_all stops sending, and a run still creating its agent stops
    there. After that, and once sending is over, Ctrl+C only prints a note, so the wait for the requests in flight
    (which may be billed already), their recording, the agent's deletion and the run's files aren't cut short. Python
    runs signal handlers on the main thread only, so this applies there, and only where Ctrl+C would otherwise raise
    KeyboardInterrupt."""

    NOTE = ("Ctrl+C: the run is finishing (the requests in flight, the agent's deletion, the run's files) and doesn't stop "
            "for it; it ends on its own.")

    def __init__(self) -> None:
        self.armed = True  # until the first Ctrl+C, or until sending is over
        self._previous = None

    def _handler(self, signum, frame) -> None:
        if self.armed:
            self.armed = False
            raise KeyboardInterrupt
        try:
            print(self.NOTE, file=sys.stderr, flush=True)
        except (RuntimeError, OSError, ValueError):  # printing inside a signal handler can fail; the note isn't vital
            pass

    def __enter__(self) -> "_CtrlC":
        if threading.current_thread() is threading.main_thread() and signal.getsignal(signal.SIGINT) is signal.default_int_handler:
            self._previous = signal.signal(signal.SIGINT, self._handler)
        return self

    def __exit__(self, *exc) -> None:
        if self._previous is not None:
            signal.signal(signal.SIGINT, self._previous)
            self._previous = None


def send_all(client, jobs: list[tuple[str, dict, dict | None]], a, wd: pathlib.Path,
             ctrl_c: _CtrlC | None = None) -> tuple[list[dict], str | None, bool]:
    """Send every request once (a few at a time with --concurrency) and record each answer as it comes back.

    An error every request would get stops the run, and so does Ctrl+C: requests not sent yet aren't sent, a request
    waiting to retry isn't tried again, and the ones in flight finish and are recorded, as are the answers already back.
    Any other error here also stops the sending before it's raised. Returns the rows in the lines' order, why the run
    stopped early (or None), and whether that was Ctrl+C."""
    rows: dict[str, dict] = {}
    stopped: str | None = None
    interrupted = False
    stop = threading.Event()

    def task(request: dict, extra: dict | None):
        if stop.is_set():
            return None  # the run stopped before this request was sent
        try:
            return one.send(client, request, retries=a.retries, extra_body=extra, stop=stop)
        except one.Stop:
            stop.set()
            raise

    pool = cf.ThreadPoolExecutor(max_workers=max(1, a.concurrency))
    futures: dict[cf.Future, str] = {}

    def collect(fut) -> None:
        nonlocal stopped
        if fut.cancelled() or futures[fut] in rows:
            return
        try:
            outcome = fut.result()
        except one.Stop as ex:
            stopped = stopped or str(ex)
            outcome = ex.outcome
        if outcome is not None:
            _record(wd, a, futures[fut], outcome, rows)

    try:
        for cid, req, extra in jobs:  # submitted inside the try, so Ctrl+C here also cancels what's queued
            futures[pool.submit(task, req, extra)] = cid
        pending = set(futures)
        while pending:
            # Short waits, so Ctrl+C is noticed while requests are in flight (on Windows, a wait without a timeout may not be).
            done, pending = cf.wait(pending, timeout=0.5, return_when=cf.FIRST_COMPLETED)
            for fut in done:
                collect(fut)
            if stop.is_set():
                for f in pending:
                    f.cancel()
    except KeyboardInterrupt:
        interrupted, stopped = True, "interrupted (Ctrl+C)"
    finally:
        stop.set()  # nothing more is sent, and a request waiting to retry isn't tried again
        if ctrl_c is not None:
            ctrl_c.armed = False  # from here on, Ctrl+C doesn't cut short the wait or the recording
        busy = sum(1 for f in futures if f.running())
        if interrupted and busy:
            print(f"Ctrl+C: nothing more is sent. Waiting for up to {busy} request(s) in flight, which may be billed already, "
                  "so their answers are kept.", file=sys.stderr, flush=True)
        pool.shutdown(wait=True, cancel_futures=True)
        for fut in list(futures):
            if fut.done():
                collect(fut)
    return [rows[cid] for cid, _, _ in jobs if cid in rows], stopped, interrupted


def cmd_run(a) -> int:
    try:
        return _run(a)
    except KeyboardInterrupt:
        print("Interrupted (Ctrl+C). This command doesn't resume: run it again with a new --workdir.", file=sys.stderr)
        return exits.INTERRUPTED


def _connect(a, reqs: list[tuple[str, dict]], calls, timeout: float):
    """The client the lines are sent with, and each line's job: (custom_id, request, extra body). In agent mode also the
    project, and the agent this run creates, with one version per distinct definition."""
    if a.mode == "request":
        if not a.base_url:
            raise SystemExit("--mode request needs --base-url https://<resource>.openai.azure.com/openai/v1/")
        import batch as batchlib

        client = batchlib.make_client(a.base_url, a.subscription, timeout=timeout, max_retries=0)
        return client, [(cid, body, None) for cid, body in reqs], None, None
    if not a.project_endpoint:
        raise SystemExit("--mode agent needs --project-endpoint https://<resource>.services.ai.azure.com/api/projects/<project>")
    project = prompt_agent.Project(a.project_endpoint, a.subscription)
    agent = {"name": a.agent_name or _agent_name(), "versions": {}}
    if project.exists(agent["name"]):
        raise SystemExit(f"The project already has an agent named '{agent['name']}'. Pass another --agent-name; nothing was changed.")
    client = project.openai_client(timeout)
    jobs = []
    try:
        for cid, definition, given, call in calls:
            key = prompt_agent.definition_key(definition)
            if key not in agent["versions"]:
                agent["versions"][key] = project.create_version(agent["name"], definition)
                print(f"Created agent '{agent['name']}' version {agent['versions'][key]}: model {definition['model']}, "
                      f"tools {[t.get('type') for t in definition['tools']]} attached directly.", flush=True)
            jobs.append((cid, {"input": given, **call}, prompt_agent.reference(agent["name"], agent["versions"][key])))
    except BaseException:
        print(f"Agent '{agent['name']}': {project.delete(agent['name'], list(agent['versions'].values()))}")
        raise
    return client, jobs, project, agent


def _delete_agent(project, agent: dict | None, a) -> None:
    if agent is None:
        return
    if a.keep_agent:
        agent["cleanup"] = "kept (--keep-agent): delete it in the project when you're done"
    else:
        agent["cleanup"] = project.delete(agent["name"], list(agent["versions"].values()))
    print(f"Agent '{agent['name']}': {agent['cleanup']}")


def _run(a) -> int:
    wd = pathlib.Path(a.workdir)
    if (wd / METRICS).exists():
        raise SystemExit(f"{wd} already holds a run ({METRICS}). Use a new --workdir, so nothing is sent twice.")
    if a.records and not a.deployment:
        raise SystemExit("run needs --deployment: the model deployment the requests name.")
    lines = load_lines(a)
    reqs, changes = requests_for(a, lines)
    calls = None
    if a.mode == "agent":
        calls, dropped = agent_calls(reqs)
        changes += _not_sent(dropped)
    wd.mkdir(parents=True, exist_ok=True)
    timeout = float(_S["client_timeout_seconds"])
    run = {"command": "agent_web_search.py run", "mode": a.mode, "input": "lines" if a.lines else f"records, design {a.design}",
           "lines": len(reqs), "changes": changes, "retries": a.retries, "concurrency": a.concurrency, "started_utc": now()}
    print(f"{len(reqs)} line(s), {a.mode} mode: each sent once, as one HTTPS request.")
    _changes(changes)
    with _CtrlC() as ctrl_c:
        client, jobs, project, agent = _connect(a, reqs, calls, timeout)
        try:
            rows, stopped, interrupted = send_all(client, jobs, a, wd, ctrl_c)
        finally:
            ctrl_c.armed = False  # sending is over: Ctrl+C doesn't cut the agent's deletion short
            _delete_agent(project, agent, a)
        write_jsonl(wd / METRICS, rows)  # appended as answers came back; rewritten in the lines' order
        responses = [json.loads(x)["response"] for x in (wd / RESPONSES).read_text(encoding="utf-8").splitlines() if x.strip()] if (wd / RESPONSES).exists() else []
        answered = [r for r in responses if r]
        run.update(
            finished_utc=now(),
            stopped=stopped,
            not_sent=len(reqs) - len(rows),
            agent={"name": agent["name"], "versions": list(agent["versions"].values()), "cleanup": agent.get("cleanup")} if agent else None,
            models=sorted({str(r.get("model")) for r in answered if r.get("model")}),
            service_tiers=sorted({str(r.get("service_tier")) for r in answered if r.get("service_tier")}),
            tools=sorted({str(t.get("type")) for _, b in reqs for t in b.get("tools") or []}),
            summary=one.summarize(rows),
        )
        write_json(wd / RUN, run)
        (wd / SUMMARY).write_text(one.summary_markdown(run, rows), encoding="utf-8", newline="\n")
    print()
    print(one.table(rows))
    s = run["summary"]
    print(f"\n{s['http_requests']} HTTP request(s) for {s['lines']} line(s); tool calls the client had to answer: {s['client_tool_calls']}.")
    if stopped:
        print(f"Stopped: {stopped}. Lines not sent: {run['not_sent']}.", file=sys.stderr)
    print(f"Saved {RUN}, {METRICS} and {SUMMARY} (counts only; rows are keyed by each line's custom_id) and {RESPONSES} "
          f"(whole responses: keep it local) in {wd}.")
    if interrupted:
        print("This command doesn't resume: run it again with a new --workdir, and every line is sent again.", file=sys.stderr)
        return exits.INTERRUPTED
    return 0 if not stopped and s["completed"] == s["lines"] else 1


def cmd_report(a) -> int:
    wd = pathlib.Path(a.workdir)
    rows = read_jsonl(wd / METRICS)
    run = read_json(wd / RUN) if (wd / RUN).exists() else {"mode": rows[0].get("mode") if rows else None}
    (wd / SUMMARY).write_text(one.summary_markdown(run, rows), encoding="utf-8", newline="\n")
    print(one.table(rows))
    print(f"\nWrote {wd / SUMMARY}.")
    return 0


def inputs(p) -> None:
    p.add_argument("records", nargs="?", help="records JSONL, built into option 1's lines; or give --lines instead")
    p.add_argument("--lines", help="a Batch input file (JSONL of Responses API lines) to send instead of records")
    p.add_argument("--first", type=int, default=int(_S["first"]), help=f"send the first N lines (default {_S['first']}; 0 for all)")
    p.add_argument("--workload", default=_D["workload"], help="records only: brand-owner, or module:ClassName")
    p.add_argument("--allow-problems", action="store_true", help="records only: continue when records have data problems")
    p.add_argument("--design", choices=DESIGNS, default=DESIGNS[0],
                   help="records only: web-search (the line as a batch job runs today, no cap; default) or bounded-web-search "
                        "(option 1's line: a cap, low effort, a schema and a search budget)")
    p.add_argument("--deployment", help="the model deployment the requests name (records are built with it; with --lines, it "
                                        "replaces each line's model, as a change)")
    p.add_argument("--max-tool-calls", type=int, help="records: option 1's cap (bounded-web-search); --lines: a change to every line")
    p.add_argument("--reasoning-effort", help="records: option 1's effort (bounded-web-search), or a change to web-search lines; "
                                              "--lines: a change to every line")
    p.add_argument("--max-output-tokens", type=int, help="records: option 1's guardrail; --lines: a change to every line")
    p.add_argument("--tool", choices=one.WEB_SEARCH_TOOLS, help="change every line's web search tool to this type, for example "
                                                                "web_search_preview to web_search")
    p.add_argument("--service-tier", choices=("default", "flex", "priority"),
                   help="flex: Flex processing, on a Flex-capable model such as gpt-6-sol")
    p.add_argument("--count-sources", action="store_true", help=f"add include {one.SOURCES}, to count the sources each search used")
    p.add_argument("--mode", choices=("request", "agent"), default="request",
                   help="request: the deployment's Responses API (default); agent: a prompt agent with web_search attached directly")


def main() -> None:
    ap, sub = cli.parser("python agent_web_search.py", __doc__)

    p = sub.add_parser("plan", help="the request run would send for the first line; calls nothing")
    inputs(p)
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("run", help="send each line once, as one request, and measure what happened inside it")
    inputs(p)
    p.add_argument("--workdir", required=True, help="a new folder for this run's files")
    p.add_argument("--base-url", help="request mode: https://<resource>.openai.azure.com/openai/v1/")
    p.add_argument("--project-endpoint", help="agent mode: https://<resource>.services.ai.azure.com/api/projects/<project>")
    p.add_argument("--agent-name", help=f"agent mode: the agent's name, which mustn't exist yet; give runs you start at the same "
                                        f"time different names (default {_S['agent_name_prefix']}-<UTC time>-<random>)")
    p.add_argument("--keep-agent", action="store_true", help="agent mode: keep the agent after the run instead of deleting it")
    p.add_argument("--retries", type=int, default=int(_S["retries"]),
                   help=f"extra tries after HTTP 408, 429 or 5xx, honoring Retry-After (default {_S['retries']}: send once and show "
                        "what happened); a request that timed out is never sent again, because it may have been billed")
    p.add_argument("--concurrency", type=int, default=1, help="requests in flight at once (default 1)")
    cli.subscription(p)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("report", help="a run's table again, and its summary.md")
    p.add_argument("workdir", help="the run's --workdir")
    p.set_defaults(fn=cmd_report)

    cli.run(ap)


if __name__ == "__main__":
    main()
