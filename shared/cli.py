"""Command-line options the entry scripts share.

Each option folder has one entry script, which fixes the design or the executor: option-1-bounded-web-search/
bounded_web_search.py, option-2-two-stage-web-iq/two_stage.py and flex-web-search-and-web-iq/flex_job.py. They all run
the shared job runner, pipeline.py. Every default comes from settings.toml.
"""
from __future__ import annotations

import argparse

from prices import PRICES_FILE_ENV
from settings import SETTINGS

_D, _W, _B, _X = SETTINGS["defaults"], SETTINGS["webiq"], SETTINGS["batch"], SETTINGS["flex"]
FLEX_NEEDS = "Flex needs a Global Standard deployment of a Flex-capable model, for example gpt-6-sol; pass --price-model gpt-6-sol."


def parser(prog: str, description: str):
    """An argument parser with sub-commands; the description is usually the entry script's docstring."""
    ap = argparse.ArgumentParser(prog=prog, description=description, formatter_class=argparse.RawDescriptionHelpFormatter)
    return ap, ap.add_subparsers(dest="cmd", required=True)


def records(p) -> None:
    p.add_argument("records", help="records JSONL, one object per line with an id")
    p.add_argument("--workload", default=_D["workload"], help="brand-owner, or module:ClassName (docs/10-adapt-to-your-workload.md)")
    p.add_argument("--allow-problems", action="store_true", help="continue when records have data problems")


def plan(p) -> None:
    p.add_argument("--plan", default=_D["plan"], help=f"two-stage search plan: lean or full (default {_D['plan']})")


def model(p, *, executor: str, deployment_required: bool, web_search: bool) -> None:
    """The deployment, the prices and the line settings. The entry script fixes the executor: batch or flex."""
    p.set_defaults(executor=executor)
    if executor == "flex":
        deployment = "a Global Standard deployment of a Flex-capable model, for example gpt-6-sol"
        price_model = f"model whose Flex rates are used (a key under [pricing.models] in settings.toml). {FLEX_NEEDS}"
    else:
        deployment = "Global Batch deployment name"
        price_model = "model whose Global Batch rates are used (a key under [pricing.models] in settings.toml)"
    p.add_argument("--deployment", required=deployment_required, default=None if deployment_required else _D["deployment"], help=deployment)
    p.add_argument("--price-model", default=_D["price_model"], help=price_model)
    p.add_argument("--prices", help="price table to use instead of live list prices: a file saved with shared/prices.py --save, "
                                     f"edited for negotiated rates if you like (or set {PRICES_FILE_ENV})")
    p.add_argument("--reasoning-effort", default=_D["reasoning_effort"])
    p.add_argument("--max-output-tokens", type=int, default=int(_D["max_output_tokens"]), help="guardrail; you pay for tokens generated, not the cap")
    if web_search:
        p.add_argument("--max-tool-calls", type=int, default=int(_D["max_tool_calls"]), help="bounded-web-search: tool calls per line")
    p.add_argument("--output-tokens-per-line", type=int, help="forecast assumption (default: [forecast.output_tokens_per_line] in settings.toml)")


def forecast(p) -> None:
    """plan: the search settings the forecast assumes, and an option to list the planned searches."""
    p.add_argument("--max-results", type=int, default=int(_W["max_results"]), help="results per search (Web IQ maxResults)")
    p.add_argument("--max-chars", type=int, default=int(_W["max_chars"]), help="characters per result")
    p.add_argument("--show-searches", action="store_true", help="list every planned search")


def job(p, *, batch: bool) -> None:
    """run: the work folder and the endpoint; for Global Batch, how to wait for the job."""
    p.add_argument("--workdir", required=True, help="folder for this job's state, results and working files")
    p.add_argument("--label")
    p.add_argument("--base-url", required=True, help="Azure OpenAI v1 endpoint: https://<resource>.openai.azure.com/openai/v1/")
    if batch:
        p.add_argument("--poll-seconds", type=int, default=int(_B["poll_seconds"]))
        p.add_argument("--stall-minutes", type=int, default=int(_B["stall_minutes"]), help="warn when no line completes or fails for this long")
        p.add_argument("--cancel-on-stall", action="store_true")
    p.add_argument("--keep-working-files", action="store_true", help="keep evidence.jsonl and batch-input.jsonl (they hold Web IQ content)")


def subscription(p) -> None:
    p.add_argument("--subscription", help="pin the Azure CLI credential to this subscription")


def max_cost(p) -> None:
    p.add_argument("--max-cost", type=float, help="two-stage: stop before spending more if a forecast (USD) is higher")


def flex(p) -> None:
    p.add_argument("--flex-concurrency", type=int, default=int(_X["concurrency"]),
                   help="requests in flight at once; size it to the deployment's tokens-per-minute quota")
    p.add_argument("--retry-failed", action="store_true",
                   help="send lines that used up their attempts (HTTP 408, 429, 5xx) again, in a new round")
    p.add_argument("--retry-ambiguous", action="store_true",
                   help="send lines again whose request timed out, lost its connection, or was in flight when an earlier run "
                        "stopped (it may have been processed and billed)")


def run(ap) -> None:
    """Parse the command line and run the chosen command."""
    a = ap.parse_args()
    a.fn(a)
