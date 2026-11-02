r"""Flex processing with web search or with Web IQ: send option 1's or option 2's lines as one synchronous request each,
with service_tier "flex", to a Global Standard deployment of a Flex-capable model, instead of one Global Batch job.

  plan    --design two-stage: the search plan and a forecast at Flex rates; bounded-web-search: the prompt tokens
  run     a whole job on Flex; re-run the same command to resume (completed lines are never sent again)

  --design bounded-web-search   Flex with web search: option 1's lines (../option-1-bounded-web-search)
  --design two-stage            Flex with Web IQ: option 2's stage 1, then its no-tools lines (../option-2-two-stage-web-iq)

flex.py, in this folder, sends the requests: pacing, at most 3 attempts per line in a round, the attempt ledger
flex-attempts.jsonl, and held lines.

Examples, from this folder:
  python flex_job.py run ../samples/brand-owner/records.jsonl --design bounded-web-search --workdir runs/flex-bounded \
      --deployment <Flex-capable Global Standard deployment> --price-model gpt-6-sol \
      --base-url https://<resource>.openai.azure.com/openai/v1/ --flex-concurrency 2
  python flex_job.py run ../samples/brand-owner/records.jsonl --design two-stage --plan lean --workdir runs/flex-two-stage \
      --deployment <Flex-capable Global Standard deployment> --price-model gpt-6-sol \
      --base-url https://<resource>.openai.azure.com/openai/v1/ --toolbox-url "<project endpoint>/toolboxes/<name>/mcp?api-version=v1" \
      --flex-concurrency 2 --max-cost 1
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "shared"))  # the code every option shares

import cli
import folders
from pipeline import cmd_plan, cmd_run

DESIGNS = ("bounded-web-search", "two-stage")


def design(p) -> None:
    p.add_argument("--design", choices=DESIGNS, required=True,
                   help="bounded-web-search: Flex with web search (option 1's lines); two-stage: Flex with Web IQ (option 2)")


def main() -> None:
    add_search_args = folders.load(folders.OPTION_2, "webiq").add_search_args  # stage 1 of Flex with Web IQ is option 2's
    ap, sub = cli.parser("python flex_job.py", __doc__)

    p = sub.add_parser("plan", help="forecast at Flex rates (two-stage) or prompt tokens (bounded-web-search); calls no model or search service")
    cli.records(p)
    design(p)
    cli.plan(p)
    cli.model(p, executor="flex", deployment_required=False, web_search=True)
    cli.forecast(p)
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("run", help="a whole job on Flex; re-run the same command to resume")
    cli.records(p)
    design(p)
    cli.plan(p)
    cli.model(p, executor="flex", deployment_required=True, web_search=True)
    cli.job(p, batch=False)
    add_search_args(p)
    cli.max_cost(p)
    cli.flex(p)
    p.set_defaults(fn=cmd_run)

    cli.run(ap)


if __name__ == "__main__":
    main()
