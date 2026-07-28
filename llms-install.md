# Portal MCP Server — AI Agent Install Guide

Copy the prompt below and paste it into your AI agent to install Portal MCP Server.

---

> Please install Portal MCP Server — an MCP server for managing interactive programs (SSH, GDB, REPL, etc.):
>
> 1. Install the Python package from the current directory:
>    ```bash
>    pip install -e .
>    ```
>    Requirements: Python 3.11+, `mcp`, `aiosqlite`.
>
> 2. Add Portal to the MCP configuration. Use the appropriate method for this agent harness:
>    - **Claude Code**: `claude mcp add portal -- python -m portal_mcp.server`
>    - **Claude Desktop / VS Code**: add to `mcpServers` in settings:
>      ```json
>      "portal": {
>        "type": "stdio",
>        "command": "python",
>        "args": ["-m", "portal_mcp.server"]
>      }
>      ```
>    - **Other**: equivalent `mcpServers` entry per the client's config format.
>
> 3. Restart the agent to load Portal. Verify by calling `process_list` — it should return "No managed processes" (empty, no errors).
>
> 4. If any step fails, read `README.md` for manual setup instructions.
