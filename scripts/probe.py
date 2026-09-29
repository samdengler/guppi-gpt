#!/usr/bin/env python3
"""Probe an MCP endpoint over streamable HTTP and print what comes back.

    uv run -- python scripts/probe.py http://localhost:8000/mcp
    ../guppi-gpt/scripts/test-token.sh | uv run -- python scripts/probe.py <url> --token -

Runs the initialize handshake, then tools/list, tools/call on the show_card tool (the
first tool whose name ends in `show_card`, so a gateway prefix is found), resources/list,
and resources/read on ui://mcp-app/card. The two list calls follow `nextCursor` and print
every page's items together with the page count. Each result is printed as the JSON the server
sent, with long strings shortened, so fields an intermediary drops or adds (tool `_meta`,
the embedded resource, `structuredContent`) are visible as they are. The client is plain
JSON-RPC over HTTP with the standard library, so no SDK parsing hides anything.

The bearer token is read from stdin with `--token -` (or taken from `--token <value>`); it
is sent only in the Authorization header and never printed.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any

PROTOCOL_VERSION = "2025-06-18"
CARD_URI = "ui://mcp-app/card"
SHORTEN = 160


class ProbeError(Exception):
    pass


class McpHttp:
    def __init__(self, url: str, token: str | None) -> None:
        self.url = url
        self.token = token
        self.session_id: str | None = None
        self.protocol_version: str | None = None
        self.next_id = 1

    def _post(self, message: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
        }
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        if self.protocol_version:
            headers["mcp-protocol-version"] = self.protocol_version
        request = urllib.request.Request(
            self.url, data=json.dumps(message).encode(), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def notify(self, method: str, params: dict[str, Any] | None = None) -> int:
        status, _, _ = self._post({"jsonrpc": "2.0", "method": method, "params": params or {}})
        return status

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        status, headers, body = self._post(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        )
        lowered = {k.lower(): v for k, v in headers.items()}
        if "mcp-session-id" in lowered:
            self.session_id = lowered["mcp-session-id"]
        text = body.decode(errors="replace")
        if status >= 400:
            raise ProbeError(f"HTTP {status}: {text[:500]}")
        for message in parse_messages(lowered.get("content-type", ""), text):
            if message.get("id") == request_id:
                if "error" in message:
                    raise ProbeError(f"JSON-RPC error: {json.dumps(message['error'])}")
                return message.get("result", {})
        raise ProbeError(f"no response for id {request_id} in HTTP {status}: {text[:500]}")


def parse_messages(content_type: str, text: str) -> list[dict[str, Any]]:
    """JSON-RPC messages from a JSON body or a server-sent event stream."""
    if content_type.startswith("text/event-stream"):
        messages = []
        for event in text.replace("\r\n", "\n").split("\n\n"):
            data = "\n".join(
                line[5:].lstrip() for line in event.split("\n") if line.startswith("data:")
            )
            if data:
                messages.append(json.loads(data))
        return messages
    parsed = json.loads(text) if text.strip() else []
    return parsed if isinstance(parsed, list) else [parsed]


def list_all(client: McpHttp, method: str, key: str) -> dict[str, Any]:
    """Every page of a list method; the gateway pages by target and returns nextCursor."""
    pages = []
    params: dict[str, Any] = {}
    while True:
        page = client.request(method, params)
        pages.append(page)
        cursor = page.get("nextCursor")
        if not cursor or len(pages) >= 20:
            break
        params = {"cursor": cursor}
    return {key: [item for page in pages for item in page.get(key, [])], "pages": len(pages)}


def shorten(value: Any) -> Any:
    if isinstance(value, str) and len(value) > SHORTEN:
        return f"{value[:SHORTEN]}... ({len(value)} chars)"
    if isinstance(value, dict):
        return {k: shorten(v) for k, v in value.items()}
    if isinstance(value, list):
        return [shorten(v) for v in value]
    return value


def show(label: str, value: Any) -> None:
    print(f"== {label}")
    print(json.dumps(shorten(value), indent=2, ensure_ascii=False))
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("url", help="the MCP endpoint, for example http://localhost:8000/mcp")
    parser.add_argument("--token", help="bearer token, or - to read it from stdin")
    parser.add_argument("--title", default="Hello")
    parser.add_argument("--body", default="It works")
    args = parser.parse_args()

    token = sys.stdin.readline().strip() if args.token == "-" else args.token
    client = McpHttp(args.url, token or None)
    failures = 0

    try:
        init = client.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "guppi-mcp-app-probe", "version": "0.1.0"},
            },
        )
    except ProbeError as error:
        print(f"== initialize\nFAILED {error}")
        return 1
    show("initialize", init)
    client.protocol_version = init.get("protocolVersion", PROTOCOL_VERSION)
    client.notify("notifications/initialized")

    tool_name = "show_card"
    try:
        listed = list_all(client, "tools/list", "tools")
        show("tools/list", listed)
        names = [t.get("name", "") for t in listed.get("tools", [])]
        tool_name = next((n for n in names if n.endswith("show_card")), tool_name)
    except ProbeError as error:
        failures += 1
        print(f"== tools/list\nFAILED {error}\n")

    calls: list[tuple[str, str, dict[str, Any]]] = [
        (
            f"tools/call {tool_name}",
            "tools/call",
            {"name": tool_name, "arguments": {"title": args.title, "body": args.body}},
        ),
        ("resources/list", "resources/list", {}),
        (f"resources/read {CARD_URI}", "resources/read", {"uri": CARD_URI}),
    ]
    for label, method, params in calls:
        try:
            if method == "resources/list":
                show(label, list_all(client, method, "resources"))
                continue
            show(label, client.request(method, params))
        except ProbeError as error:
            failures += 1
            print(f"== {label}\nFAILED {error}\n")

    print(f"probe: {4 - failures} of 4 calls answered")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
