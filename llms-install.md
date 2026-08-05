# Portal MCP Server — AI Agent Install Guide

Copy the prompt below and paste it into your AI agent to install Portal MCP Server.

---

> Please install Portal MCP Server — an MCP server for managing interactive programs (SSH, GDB, psql, python REPL, etc.) via MCP tools:
>
> 1. No package installation is needed — Portal runs directly with `uv`:
>    ```bash
>    uv sync   # run once inside the Portal repo: creates .venv with all deps
>    ```
>    Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+. Dependencies
>    (managed by uv from `pyproject.toml`): `mcp` (1.x), `aiosqlite`, `pyte`,
>    plus `pywinpty` (Windows) / `ptyprocess` (POSIX). `uv run` auto-syncs on
>    first launch, so the sync step above is optional.
>
> 2. Add Portal to the MCP configuration for this platform. Substitute
>    `<PORTAL_PATH>` with the absolute path of the Portal repo (e.g.
>    `D:\Develop\Portal` on Windows):
>
>    **Claude Code** — run:
>    ```bash
>    claude mcp add portal -- uv run --project "<PORTAL_PATH>" portal-mcp
>    ```
>
>    **Claude Desktop** — edit `claude_desktop_config.json`:
>    - macOS:   `~/Library/Application Support/Claude/`
>    - Windows: `%APPDATA%\Claude\`
>    - Linux:   `~/.config/Claude/`
>    ```json
>    {
>      "mcpServers": {
>        "portal": {
>          "type": "stdio",
>          "command": "uv",
>          "args": ["run", "--project", "<PORTAL_PATH>", "portal-mcp"]
>        }
>      }
>    }
>    ```
>
>    **Cursor** — create/edit `.cursor/mcp.json` (user-global or project-root):
>    ```json
>    {
>      "mcpServers": {
>        "portal": {
>          "command": "uv",
>          "args": ["run", "--project", "<PORTAL_PATH>", "portal-mcp"]
>        }
>      }
>    }
>    ```
>
>    **Windsurf** — edit `~/.codeium/windsurf/mcp_config.json`:
>    ```json
>    {
>      "mcpServers": {
>        "portal": {
>          "command": "uv",
>          "args": ["run", "--project", "<PORTAL_PATH>", "portal-mcp"]
>        }
>      }
>    }
>    ```
>
>    **OpenCode** — edit `~/.opencode/mcp.json` (note: uses `"servers"` key):
>    ```json
>    {
>      "servers": {
>        "portal": {
>          "command": "uv",
>          "args": ["run", "--project", "<PORTAL_PATH>", "portal-mcp"],
>          "transport": "stdio"
>        }
>      }
>    }
>    ```
>
>    **Codex** — edit `~/.codex/config.toml` (TOML format):
>    ```toml
>    [mcp_servers.portal]
>    command = "uv"
>    args = ["run", "--project", "<PORTAL_PATH>", "portal-mcp"]
>    ```
>
>    **VS Code / GitHub Copilot** — create/edit `.vscode/mcp.json` (note: uses `"servers"` key):
>    ```json
>    {
>      "servers": {
>        "portal": {
>          "type": "stdio",
>          "command": "uv",
>          "args": ["run", "--project", "<PORTAL_PATH>", "portal-mcp"]
>        }
>      }
>    }
>    ```
>
>    **Cline / Roo Code** — edit `cline_mcp_settings.json`:
>    ```json
>    {
>      "mcpServers": {
>        "portal": {
>          "command": "uv",
>          "args": ["run", "--project", "<PORTAL_PATH>", "portal-mcp"]
>        }
>      }
>    }
>    ```
>
>    **Gemini CLI** — edit `~/.gemini/settings.json`:
>    ```json
>    {
>      "mcpServers": {
>        "portal": {
>          "command": "uv",
>          "args": ["run", "--project", "<PORTAL_PATH>", "portal-mcp"]
>        }
>      }
>    }
>    ```
>
> 3. Restart the agent to load Portal. The first launch runs `uv sync`
>    automatically, then starts the server. Verify by calling `process_list`
>    — it should return "No managed processes" (no error, empty list).
>
> 4. If any step fails, read `README.md` or `README_zh.md` for manual setup instructions.
