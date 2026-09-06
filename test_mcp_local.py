# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "httpx>=0.27",
#   "python-dotenv>=1.0",
# ]
# ///
"""
Integration test for the WhatsApp MCP server using a *local* Ollama model.

No API key required — just Ollama running with a tool-capable model.

Prerequisites
─────────────
1.  docker compose up -d          (all services running)
2.  WhatsApp authenticated         (scan QR at http://localhost:8100/)
3.  Ollama installed and running   https://ollama.com
4.  A tool-capable model pulled:
      ollama pull qwen2.5:7b       (good balance of speed and tool use)
      ollama pull llama3.2:3b      (faster, lighter)

Run
───
  uv run test_mcp_local.py
  uv run test_mcp_local.py --model llama3.2:3b
  uv run test_mcp_local.py --mcp-url http://localhost:8000/sse

How it works
────────────
Ollama exposes an OpenAI-compatible API that supports function/tool calling.
We fetch the tool schemas directly from the MCP server's JSON-RPC endpoint,
then run a manual tool-call loop:
  1. Send user prompt + tool definitions to Ollama
  2. Ollama replies with a tool_calls block
  3. We call the MCP server tool via its HTTP API
  4. Feed the result back to Ollama for a natural-language summary
  5. Print the final answer

Note on "native MCP" in local models
─────────────────────────────────────
No local model runtime currently speaks the MCP protocol natively — MCP is a
higher-level JSON-RPC spec that sits on top of tool calling.  The pattern above
is the standard workaround: extract the tool schemas from MCP and feed them as
OpenAI-style function definitions to any model that supports function calling.
Claude Desktop / Claude Code handle the MCP ↔ tool-calling translation for you;
here we do it ourselves.
"""

import argparse
import json
import sys
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

MCP_DEFAULT = "http://localhost:8000"
OLLAMA_DEFAULT = "http://localhost:11434"
MODEL_DEFAULT = "qwen2.5:7b"


# ── MCP helpers ───────────────────────────────────────────────────────────────


def mcp_rpc(mcp_base: str, method: str, params: dict | None = None) -> Any:
    """Send a JSON-RPC 2.0 request to the MCP server."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    r = httpx.post(f"{mcp_base}/rpc", json=payload, timeout=10)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"MCP error: {data['error']}")
    return data["result"]


def fetch_tool_schemas(mcp_base: str) -> list[dict]:
    """Return tools as OpenAI-compatible function definitions."""
    result = mcp_rpc(mcp_base, "tools/list")
    tools = []
    for tool in result.get("tools", []):
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get(
                        "inputSchema", {"type": "object", "properties": {}}
                    ),
                },
            }
        )
    return tools


def call_mcp_tool(mcp_base: str, name: str, arguments: dict) -> str:
    """Invoke an MCP tool and return the result as a JSON string."""
    result = mcp_rpc(mcp_base, "tools/call", {"name": name, "arguments": arguments})
    content = result.get("content", [])
    # MCP returns a list of content blocks; join text blocks
    text_parts = [c["text"] for c in content if c.get("type") == "text"]
    return "\n".join(text_parts) if text_parts else json.dumps(result)


# ── Ollama tool-call loop ─────────────────────────────────────────────────────


def run_with_ollama(
    prompt: str,
    tools: list[dict],
    mcp_base: str,
    ollama_base: str,
    model: str,
) -> str:
    """Run a prompt through Ollama with a tool-call loop; return final text."""
    messages: list[dict] = [{"role": "user", "content": prompt}]

    with httpx.Client(base_url=ollama_base, timeout=120) as client:
        for _ in range(5):  # max tool-call rounds
            resp = client.post(
                "/api/chat",
                json={
                    "model": model,
                    "messages": messages,
                    "tools": tools,
                    "stream": False,
                },
            )
            resp.raise_for_status()
            reply = resp.json()["message"]
            messages.append(reply)

            tool_calls = reply.get("tool_calls") or []
            if not tool_calls:
                return reply.get("content", "")

            # Execute every tool the model requested
            for tc in tool_calls:
                fn = tc["function"]
                tool_result = call_mcp_tool(
                    mcp_base, fn["name"], fn.get("arguments", {})
                )
                messages.append(
                    {"role": "tool", "content": tool_result, "name": fn["name"]}
                )

    return "Max tool-call rounds reached."


# ── Test cases ────────────────────────────────────────────────────────────────


TESTS = [
    ("List chats", "List my WhatsApp chats. Show the chat names and last messages."),
    ("Search contacts", "Search for a contact named 'John' in my WhatsApp contacts."),
]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="WhatsApp MCP integration test (Ollama)"
    )
    parser.add_argument("--mcp-url", default=MCP_DEFAULT, help="MCP server base URL")
    parser.add_argument("--ollama-url", default=OLLAMA_DEFAULT, help="Ollama base URL")
    parser.add_argument("--model", default=MODEL_DEFAULT, help="Ollama model name")
    args = parser.parse_args()

    print(f"MCP server : {args.mcp_url}")
    print(f"Ollama     : {args.ollama_url}  model={args.model}")

    # Verify Ollama is reachable
    try:
        httpx.get(f"{args.ollama_url}/api/tags", timeout=5).raise_for_status()
    except Exception as exc:
        sys.exit(
            f"ERROR: Cannot reach Ollama at {args.ollama_url} — {exc}\n"
            "Install from https://ollama.com and run: ollama pull qwen2.5:7b"
        )

    # Fetch tool schemas once
    try:
        tools = fetch_tool_schemas(args.mcp_url)
    except Exception as exc:
        sys.exit(
            f"ERROR: Cannot reach MCP server at {args.mcp_url} — {exc}\n"
            "Run: docker compose up -d"
        )

    print(f"\nFound {len(tools)} MCP tools: {[t['function']['name'] for t in tools]}\n")

    for label, prompt in TESTS:
        print("=" * 60)
        print(f"TEST : {label}")
        print(f"QUERY: {prompt}")
        print("-" * 60)
        answer = run_with_ollama(
            prompt, tools, args.mcp_url, args.ollama_url, args.model
        )
        print(f"REPLY:\n{answer}\n")


if __name__ == "__main__":
    main()
