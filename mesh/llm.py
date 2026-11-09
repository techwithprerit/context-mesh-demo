"""Optional: let a model drive the graph's MCP tools (`mesh ask --llm "..."`).

Deliberately a side door. The deterministic `ask` route answers every demo question without a
model; this exists to show that the SAME tools work for an agent. Needs ANTHROPIC_API_KEY and
`pip install anthropic`. Read-only tools only - the write tool on the topology node is never
handed to the model.
"""
from __future__ import annotations

import asyncio
import json
import os

from mcp.client.client import Client

from .common import node_url

SYSTEM = ("You are the on-call assistant for a context mesh. Answer with the tools, cite which node each fact came from "
          "and how old it is, and never guess a value you did not read. Keep answers under 120 words.")


async def _tools():
    async with Client(node_url("graph")) as c:
        r = await c.list_tools()
        return [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema} for t in r.tools
                if t.annotations and t.annotations.read_only_hint]


async def _call(name: str, args: dict) -> str:
    async with Client(node_url("graph")) as c:
        r = await c.call_tool(name, args)
        return "".join(getattr(x, "text", "") for x in r.content)[:6000]


def ask_with_llm(question: str, model: str = "claude-sonnet-4-5") -> str:
    try:
        import anthropic
    except ImportError:
        return "pip install anthropic (and set ANTHROPIC_API_KEY) to use --llm"
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return "ANTHROPIC_API_KEY not set; the deterministic route works without it: mesh ask \"...\""
    client = anthropic.Anthropic()
    tools = asyncio.run(_tools())
    messages = [{"role": "user", "content": question}]
    for _ in range(8):
        resp = client.messages.create(model=model, max_tokens=800, system=SYSTEM, tools=tools, messages=messages)
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text")
        results = []
        for b in resp.content:
            if b.type == "tool_use":
                print(f"  ↳ {b.name}({json.dumps(b.input)})")
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": asyncio.run(_call(b.name, b.input))})
        messages.append({"role": "user", "content": results})
    return "(stopped after 8 tool rounds)"
