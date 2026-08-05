# Portal MCP Server — AI Agent Install Guide

Copy the prompt below and paste it into your AI agent to install Portal MCP Server.

---

> Please install Portal MCP Server — an MCP server for managing interactive programs (SSH, GDB, psql, python REPL, etc.) via MCP tools:
>
> 1. Install the Python package from the current directory:
>    ```bash
>    pip install -e .
>    ```
>    Requires Python 3.11+. Dependencies: `mcp`, `aiosqlite`, `pyte`, plus `pywinpty` (Windows) / `ptyprocess` (POSIX).
>
> 2. Add Portal to the MCP configuration for this platform:
>
>    **Claude Code** — run:
>    ```bash
>    claude mcp add portal -- python -m portal_mcp.server
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
>          "command": "python",
>          "args": ["-m", "portal_mcp.server"]
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
>          "command": "python",
>          "args": ["-m", "portal_mcp.server"]
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
>          "command": "python",
>          "args": ["-m", "portal_mcp.server"]
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
>          "command": "python",
>          "args": ["-m", "portal_mcp.server"],
>          "transport": "stdio"
>        }
>      }
>    }
>    ```
>
>    **Codex** — edit `~/.codex/config.toml` (TOML format):
>    ```toml
>    [mcp_servers.portal]
>    command = "python"
>    args = ["-m", "portal_mcp.server"]
>    ```
>
>    **VS Code / GitHub Copilot** — create/edit `.vscode/mcp.json` (note: uses `"servers"` key):
>    ```json
>    {
>      "servers": {
>        "portal": {
>          "type": "stdio",
>          "command": "python",
>          "args": ["-m", "portal_mcp.server"]
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
>          "command": "python",
>          "args": ["-m", "portal_mcp.server"]
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
>          "command": "python",
>          "args": ["-m", "portal_mcp.server"]
>        }
>      }
>    }
>    ```
>
> 3. Restart the agent to load Portal. Then verify by calling `process_list` — it should return "No managed processes" (no error, empty list).
>
> 4. If any step fails, read `README.md` or `README_zh.md` for manual setup instructions.
