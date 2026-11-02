r"""Option 2, two-stage with Web IQ: run each unique search once with Microsoft Web IQ, then submit a no-tools Global
Batch job in which each line carries its own evidence.

  plan     the search plan and a cost forecast; calls no model or search service
  search   stage 1 only: run the searches and write an evidence file
  build    write the no-tools lines from an evidence file, without submitting them
  run      a whole job: plan, search, build, forecast, submit, wait, collect, price and clean up; re-run to resume

toolbox.py, in this folder, creates the Microsoft Foundry toolbox that stage 1 can call. To send stage 2 with Flex
processing instead of Global Batch, use ../flex-web-search-and-web-iq/flex_job.py --design two-stage.

Examples, from this folder:
  python two_stage.py plan ../samples/brand-owner/records.jsonl --plan lean --show-searches
  python two_stage.py run ../samples/brand-owner/records.jsonl --workdir runs/lean --plan lean \
      --toolbox-url "<project endpoint>/toolboxes/<name>/mcp?api-version=v1" \
      --base-url https://<resource>.openai.azure.com/openai/v1/ --deployment <batch deployment> --price-model gpt-5.4-mini --max-cost 1
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "shared"))  # the code every option shares

import cli
from pipeline import cmd_build, cmd_plan, cmd_run, cmd_search
from settings import SETTINGS
from webiq import add_search_args


def main() -> None:
    ap, sub = cli.parser("python two_stage.py", __doc__)

    p = sub.add_parser("plan", help="search plan and forecast; calls no model or search service")
    cli.records(p)
    cli.plan(p)
    cli.model(p, executor="batch", deployment_required=False, web_search=False)
    cli.forecast(p)
    p.set_defaults(fn=cmd_plan, design="two-stage")

    p = sub.add_parser("search", help="stage 1 only: run the searches, write an evidence file")
    cli.records(p)
    cli.plan(p)
    p.add_argument("--out", required=True, help="evidence JSONL (job-scoped working data; delete it after the job)")
    add_search_args(p)
    cli.auto_retry(p)
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("build", help="write the no-tools lines from an evidence file, without submitting them")
    cli.records(p)
    cli.plan(p)
    cli.model(p, executor="batch", deployment_required=True, web_search=False)
    p.add_argument("--evidence", required=True, help="evidence JSONL from the search command")
    p.add_argument("--max-chars", type=int, default=int(SETTINGS["webiq"]["max_chars"]), help="characters per evidence result in the prompt")
    p.add_argument("--out", required=True, help="where to write the batch lines (JSONL)")
    p.set_defaults(fn=cmd_build, design="two-stage")

    p = sub.add_parser("run", help="a whole job on Global Batch; re-run the same command to resume")
    cli.records(p)
    cli.plan(p)
    cli.model(p, executor="batch", deployment_required=True, web_search=False)
    cli.job(p, batch=True)
    add_search_args(p)
    cli.max_cost(p)
    p.set_defaults(fn=cmd_run, design="two-stage")

    cli.run(ap)


if __name__ == "__main__":
    main()
