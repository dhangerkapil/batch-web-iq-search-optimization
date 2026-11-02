"""Exit codes of every option's `run`: one contract, so a scheduler knows what to do next (docs/07-deploy-and-run.md#exit-codes).

Batch, Flex and the Web IQ searches retry what retrying can fix on their own (automatic rounds, quota waits), so a code
other than 0 means one of these.
"""
from __future__ import annotations

DONE = 0  # every line answered, or failed for good with an error retrying can't fix (listed in results.jsonl)
NEEDS_PERSON = 1  # bad input, sign-in or permissions, a --max-cost forecast, or every record held for a non-retryable reason
HELD = 3  # finished, but records are held after the automatic rounds: held-records.jsonl lists them
RERUN = 75  # still running, or waiting for quota or capacity: re-run the same command later (EX_TEMPFAIL)
INTERRUPTED = 130  # Ctrl+C

HELD_RECORDS = "held-records.jsonl"  # held records, in the input records format, to run again as a new job
HELD_STATUS = "held"  # a record's status in results.jsonl when it wasn't answered and may be sent again later


class Exit(SystemExit):
    """Ends `run` with one of the codes above. It reads like SystemExit("message"); cli.run prints the message to stderr
    and exits with the code."""

    def __init__(self, code: int, message: str):
        super().__init__(code)
        self.message = message

    def __str__(self) -> str:
        return self.message
