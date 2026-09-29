# guppi-mcp-app

An MCP server whose tools return [MCP Apps](https://github.com/modelcontextprotocol/ext-apps)
UI resources: `show_card` and the phase 4 experiments compared in `docs/experiments.md`.
It runs on Amazon Bedrock AgentCore Runtime and is a target on the GuppiGPT platform's
tools gateway, so the platform's Guppi agent offers it on
`https://chat.dengler.io/p/mcp-app/`.

This is a tools-only project on the platform described in guppi-gpt's
`docs/proposals/platform.md`: no page and no agent of its own, only the server, a gateway
target, and a manifest. `AGENTS.md` has the layout, the rules, and how to run and deploy it.
