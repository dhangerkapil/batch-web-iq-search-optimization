"""A Microsoft Foundry prompt agent with the web_search tool attached directly: created over REST for a run, called once
per line through the project's Responses API, and deleted after the run.

Learn: "When you add web search directly to a prompt agent or a Responses API request, it uses Web IQ. When you add web
search to a toolbox, it still uses Grounding with Bing Search, not Web IQ."
(https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/web-search). So the definition carries the tool
itself, and split_body() refuses every other tool type, including a toolbox attached as an MCP tool.

"The model and tool settings belong to the agent definition, not the invocation request" (the same page, image search).
split_body() moves a line's model, system messages, tools, output schema, reasoning effort and the other settings a
definition takes into the definition, and each call sends the line's other messages as its input. A line setting that
neither takes is reported, not sent. The REST calls are the ones that page shows:
POST {project}/agents/{name}/versions, POST {project}/openai/v1/responses with agent_reference, and
DELETE {project}/agents/{name}/versions/{version}.
"""
from __future__ import annotations

import copy
import hashlib
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "shared"))  # the code every option shares

from settings import SETTINGS

_S = SETTINGS["agent_sample"]
API = f"api-version={_S['api_version']}"
HTTP_TIMEOUT = float(_S["http_timeout_seconds"])
SYSTEM_ROLES = ("system", "developer")
WEB_SEARCH_TOOLS = ("web_search", "web_search_preview")
# Learn, Foundry Agent Service quotas and limits: "Maximum number of versions per agent | 1,000"; one more is a terminal
# HTTP 400 (https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/limits-quotas-regions).
MAX_VERSIONS = 1000
# Line settings that go into the agent's definition, with the model, the instructions and the tools: the other fields of
# a prompt agent's definition (PromptAgentDefinition in the Azure AI Projects SDK).
DEFINITION_KEYS = ("text", "reasoning", "temperature", "top_p", "tool_choice")
# Line settings each call sends, with the line's input.
CALL_KEYS = ("max_output_tokens", "max_tool_calls", "service_tier", "include", "store")
PLACED = ("model", "input", "instructions", "tools", *DEFINITION_KEYS, *CALL_KEYS)


def _text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") for p in content or [] if isinstance(p, dict))


def split_body(body: dict) -> tuple[dict, list, dict, list[str]]:
    """(definition, input, call settings, keys not sent) for a line's request body. A key the line has that neither the
    definition nor the call takes is returned in the last list, so the run can say it wasn't sent."""
    tools = copy.deepcopy(body.get("tools") or [])
    other = sorted({str(t.get("type")) for t in tools if t.get("type") not in WEB_SEARCH_TOOLS})
    if other:
        raise SystemExit(f"Agent mode attaches only the web_search tool, directly; this line also has {other}. No toolbox or "
                         "MCP tool is used: through a toolbox, web search uses Grounding with Bing Search, not Web IQ.")
    messages = body.get("input")
    messages = messages if isinstance(messages, list) else [{"role": "user", "content": messages}]
    system = [m for m in messages if isinstance(m, dict) and m.get("role") in SYSTEM_ROLES]
    rest = [m for m in messages if not (isinstance(m, dict) and m.get("role") in SYSTEM_ROLES)]
    instructions = "\n\n".join(t for t in [body.get("instructions") or "", *(_text(m) for m in system)] if t)
    definition: dict = {"kind": "prompt", "model": body.get("model"), "tools": tools}
    if instructions:
        definition["instructions"] = instructions
    for k in DEFINITION_KEYS:
        if k in body:
            definition[k] = copy.deepcopy(body[k])
    call = {k: copy.deepcopy(body[k]) for k in CALL_KEYS if k in body}
    return definition, rest, call, sorted(k for k in body if k not in PLACED)


def definition_key(definition: dict) -> str:
    """Lines whose definitions are the same share one agent version."""
    return hashlib.sha256(json.dumps(definition, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def reference(name: str, version: str) -> dict:
    """What a call sends, besides its input, to run that agent version."""
    return {"agent_reference": {"type": "agent_reference", "name": name, "version": str(version)}}


def _credential(subscription: str | None):
    from azure.identity import AzureCliCredential, DefaultAzureCredential

    return AzureCliCredential(subscription=subscription) if subscription else DefaultAzureCredential()


class Project:
    """REST calls on one Foundry project (create, check and delete agent versions), and its Responses API client."""

    def __init__(self, endpoint: str, subscription: str | None = None, http=None, credential=None):
        self.endpoint = endpoint.rstrip("/")
        self.subscription = subscription
        self._http = http  # tests pass a fake
        self._cred = credential
        self._provider = None

    def _credential(self):
        if self._cred is None:
            self._cred = _credential(self.subscription)
        return self._cred

    def _tokens(self):
        """One bearer-token provider for the REST calls and the Responses calls. It keeps the token until shortly before
        it expires, so deleting the agent after a run normally needs no new sign-in call: with Azure CLI sign-in that's
        an `az` process, which a Ctrl+C in the terminal would stop too."""
        if self._provider is None:
            from azure.identity import get_bearer_token_provider

            self._provider = get_bearer_token_provider(self._credential(), _S["foundry_scope"])
        return self._provider

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._tokens()()}", "Content-Type": "application/json"}

    def _client(self):
        if self._http is None:
            import httpx

            self._http = httpx.Client(timeout=HTTP_TIMEOUT)
        return self._http

    def exists(self, name: str) -> bool:
        r = self._client().get(f"{self.endpoint}/agents/{name}?{API}", headers=self._headers())
        if r.status_code == 404:
            return False
        if r.status_code == 200:
            return True
        raise SystemExit(f"Couldn't check whether agent '{name}' exists: HTTP {r.status_code} {r.text[:300]}")

    def create_version(self, name: str, definition: dict) -> str:
        r = self._client().post(f"{self.endpoint}/agents/{name}/versions?{API}", headers=self._headers(), json={"definition": definition})
        if r.status_code not in (200, 201):
            raise SystemExit(f"Creating agent '{name}' failed: HTTP {r.status_code} {r.text[:400]}")
        return str(r.json()["version"])

    def delete(self, name: str, versions: list[str]) -> str:
        """Delete the versions this run created, then the agent, which the run created too; say what a GET finds after."""
        try:
            for v in versions:
                self._client().delete(f"{self.endpoint}/agents/{name}/versions/{v}?{API}", headers=self._headers())
            if self.exists(name):
                self._client().delete(f"{self.endpoint}/agents/{name}?{API}", headers=self._headers())
            return "STILL PRESENT: delete it in the project" if self.exists(name) else "deleted (GET returns 404)"
        except (Exception, SystemExit) as ex:  # the run's results matter more; report the leftover instead
            return f"not confirmed deleted ({type(ex).__name__}: {str(ex)[:200]}): check the project"

    def openai_client(self, timeout: float):
        """The OpenAI client for the project's Responses API (it takes agent_reference), with the SDK's retries off."""
        from openai import OpenAI

        return OpenAI(base_url=f"{self.endpoint}/openai/v1", api_key=self._tokens(), timeout=timeout, max_retries=0)
