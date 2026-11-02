"""Workloads: everything that is specific to one kind of record.

A workload says which searches a record needs, how the batch prompt reads, which JSON schema the
answer must follow, and how to read a decision back for evaluation. The pipeline itself is generic.

Built-in:
  brand-owner   the sample workload (samples/brand-owner/)

Your own: write a subclass of Workload in a module Python can import, for example shared/workloads/my_workload.py, and
pass "workloads.my_workload:MyWorkload" (module:ClassName) to --workload. docs/10-adapt-to-your-workload.md walks
through it.
"""
from __future__ import annotations

import importlib
import json
import re
import unicodedata
from dataclasses import dataclass

STATUSES = ("verified", "contradicted", "insufficient_evidence")

LEGAL_WORDS = {
    "the", "inc", "incorporated", "plc", "ltd", "limited", "llc", "lp", "corp", "corporation", "company", "co",
    "sa", "ag", "nv", "bv", "gmbh", "kgaa", "spa", "srl", "ab", "as", "oy", "group", "holding", "holdings",
}


@dataclass(frozen=True)
class Search:
    """One web search a record needs. Identical (query, region) pairs are run once per job."""

    query: str
    purpose: str
    region: str | None = None


def name_tokens(name: str | None) -> set[str]:
    """Comparable name tokens: accents and apostrophes removed, lowercase, legal words dropped.

    "Nestlé S.A." -> {"nestle"}; "The Coca-Cola Company" -> {"coca", "cola"}; "Lay's" -> {"lays"}.
    """
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"['\u2019`.]", "", s)
    return {t for t in re.split(r"[^a-z0-9]+", s) if t and t not in LEGAL_WORDS}


def name_contains(container: str | None, part: str | None) -> bool:
    p = name_tokens(part)
    return bool(p) and p <= name_tokens(container)


class Workload:
    """Base class. Subclasses fill in the record-specific parts."""

    name = "base"
    plans: tuple[str, ...] = ("lean", "full")
    facts: tuple[str, ...] = ()
    # Evidence for these purposes is placed first in every prompt. Put searches shared by many records
    # first, so consecutive lines start with the same long prefix (a prompt-caching opportunity).
    evidence_order: tuple[str, ...] = ()

    def record_id(self, rec: dict) -> str:
        return str(rec["id"])

    def problems(self, rec: dict) -> list[str]:
        """Data-quality problems that would make the searches or the decision unreliable."""
        return []

    def searches(self, rec: dict, plan: str) -> list[Search]:
        raise NotImplementedError

    def system_prompt(self) -> str:
        raise NotImplementedError

    def rules(self) -> str:
        raise NotImplementedError

    def record_text(self, rec: dict) -> str:
        return json.dumps(rec, ensure_ascii=False, separators=(",", ":"))

    def output_format(self) -> dict:
        """The Responses API text.format object (strict JSON schema)."""
        raise NotImplementedError

    def web_search_prompts(self, rec: dict, bounded: bool) -> tuple[str, str]:
        """(system, user) for the comparison designs where the model searches inside the batch line."""
        raise NotImplementedError

    def user_country(self, rec: dict) -> str | None:
        """ISO country code passed to the built-in web search tool as user_location, if any."""
        return None

    def fact_statuses(self, result: dict | None) -> dict[str, str | None]:
        raise NotImplementedError

    def all_verified(self, result: dict | None) -> bool | None:
        return None


def normalize_status(value) -> str | None:
    """Lowercase and trim a status. Values outside STATUSES are returned as-is so evaluation can count them."""
    if value is None:
        return None
    return str(value).strip().lower().replace(" ", "_")


def first_object(result):
    """Models without a strict schema sometimes wrap the object in a one-item list."""
    if isinstance(result, list) and len(result) == 1 and isinstance(result[0], dict):
        return result[0]
    return result if isinstance(result, dict) else None


def get_workload(name: str) -> Workload:
    if name == "brand-owner":
        from .brand_owner import BrandOwner

        return BrandOwner()
    if ":" in name:
        module, cls = name.split(":", 1)
        return getattr(importlib.import_module(module), cls)()
    raise SystemExit(f"Unknown workload '{name}'. Use brand-owner or module:ClassName, for example workloads.my_workload:MyWorkload.")
