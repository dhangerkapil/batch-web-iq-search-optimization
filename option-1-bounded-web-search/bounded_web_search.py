r"""Option 1, bounded web search: batch lines in which the model searches with the built-in web_search tool, capped and
steered, run as one Global Batch job.

  plan    prompt tokens of the lines; search cost can't be forecast, because the model decides how much to search
  build   write the lines without submitting them
  run     a whole job: build, submit, wait, collect, price and clean up; re-run the same command to resume

--design web-search builds the same lines without the caps, to measure a job as it runs today. To send these lines
with Flex processing instead, use ../flex-web-search-and-web-iq/flex_job.py --design bounded-web-search.

Examples, from this folder:
  python bounded_web_search.py build ../samples/brand-owner/records.jsonl --deployment <batch deployment> --out bounded.jsonl
  python bounded_web_search.py run ../samples/brand-owner/records.jsonl --workdir runs/bounded \
      --base-url https://<resource>.openai.azure.com/openai/v1/ --deployment <batch deployment> --price-model gpt-5.4-mini
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "shared"))  # the code every option shares

import cli
from pipeline import cmd_build, cmd_plan, cmd_run

DESIGNS = ("bounded-web-search", "web-search")


def design(p) -> None:
    p.add_argument("--design", choices=DESIGNS, default=DESIGNS[0],
                   help="bounded-web-search (option 1), or web-search: the same lines without caps, as a job runs today")


def main() -> None:
    ap, sub = cli.parser("python bounded_web_search.py", __doc__)

    p = sub.add_parser("plan", help="prompt tokens of the lines; calls no model or search service")
    cli.records(p)
    design(p)
    cli.model(p, executor="batch", deployment_required=False, web_search=True)
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("build", help="write the lines without submitting them")
    cli.records(p)
    design(p)
    cli.model(p, executor="batch", deployment_required=True, web_search=True)
    p.add_argument("--out", required=True, help="where to write the batch lines (JSONL)")
    p.set_defaults(fn=cmd_build, evidence=None)

    p = sub.add_parser("run", help="a whole job on Global Batch; re-run the same command to resume")
    cli.records(p)
    design(p)
    cli.model(p, executor="batch", deployment_required=True, web_search=True)
    cli.job(p, batch=True)
    cli.subscription(p)
    p.set_defaults(fn=cmd_run, max_cost=None)

    cli.run(ap)


if __name__ == "__main__":
    main()
