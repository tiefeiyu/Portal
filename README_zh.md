# Portal MCP Server

一个专为**交互式程序**设计的 MCP（Model Context Protocol）服务器。通过 MCP 工具调用，实现交互式进程的启动、监控、I/O 读写、信号发送和生命周期管理。

**适用场景：** SSH 远程连接、GDB 调试、数据库 CLI（psql/mysql）、REPL 环境等需要持续交互的程序。

**不适用场景：** 简单的单次命令执行（如 `ls`、`echo`）——这些用 Terminal 自带的 Shell 工具即可。

## 安装

无需安装包 —— 直接用 `uv` 运行：

```bash
uv sync   # 可选：预创建 .venv；`uv run` 首次启动时自动同步
```

需要 [uv](https://docs.astral.sh/uv/) 和 Python 3.11+。依赖由 uv 根据
`pyproject.toml` 管理：`mcp`（1.x）、`aiosqlite`、`pyte`，以及
`pywinpty`（Windows）/ `ptyprocess`（POSIX）。

## 配置

添加到 MCP 客户端配置中（如 Claude Code），将 `<PORTAL_PATH>` 替换为本仓库的绝对路径：

```json
{
  "mcpServers": {
    "portal": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--project", "<PORTAL_PATH>", "portal-mcp"]
    }
  }
}
```

Claude Code 一行命令：

```bash
claude mcp add portal -- uv run --project "<PORTAL_PATH>" portal-mcp
```

环境变量 `PORTAL_DB_PATH` 可指定 SQLite 数据库路径。默认情况下每个服务实例使用自己的文件：当前目录下 `.portal/portal-<pid>-<时间戳>.db` —— 每次启动全新创建、以带时间戳的历史文件保留，且按实例命名，残留或并发的服务器实例不会锁住文件阻塞新实例启动。

## 一键安装

将 [`llms-install.md`](llms-install.md) 中的提示词复制给你的 AI Agent，即可自动完成安装和配置。

## 工具列表

| 工具 | 说明 |
|------|------|
| `process_start` | 启动子进程，可指定参数、工作目录、环境变量、超时 |
| `process_read` | 读取进程输出（stdout/stderr/stdin/both），按时间范围查询 |
| `process_write` | 向进程的 stdin 写入内容 |
| `process_signal` | 向进程发送操作系统信号 |
| `process_list` | 列出所有托管进程及其概要信息 |
| `process_inspect` | 查看单个进程的详细信息 |
| `process_kill` | 杀死单个进程（数据保留） |
| `process_kill_all` | 杀死所有托管进程 |
| `process_clear` | 清空某个进程的 I/O 记录 |
| `process_cleanup` | 移除已终止进程及其全部数据 |
| `process_screen` | 快照 PTY 进程的实时屏幕（用于全屏 TUI） |
| `program_query` | 查询程序是否需要 PTY（持久化注册表） |
| `program_record` | 记录已确认的程序事实到持久化注册表 |

### process_start

启动子进程并开始捕获其输出。

- `command`（必填）：可执行文件或命令
- `args`（可选）：命令行参数列表
- `cwd`（可选）：工作目录
- `env`（可选）：环境变量（与当前环境合并）
- `timeout_ms`（可选）：空闲超时时间（毫秒），0 表示永不超时
- `pty`（可选，默认 `false`）：使用虚拟 PTY 运行而非管道 ——
  检查 `isatty()` 的程序（ssh、gdb、psql、REPL）或全屏 TUI
  （vim、htop、less）设为 `true`；拿不准时用 `true`
  （见[虚拟 PTY 支持](#虚拟-pty-支持)）

返回：`id`、`os_pid`、`status`

### process_read

读取进程捕获的输出，同时重置空闲计时器。

- `id`（必填）：内部进程 ID
- `source`（可选）：`stdout`、`stderr`、`stdin` 或 `both`，默认 `both`
- `duration`（可选）：向前读取多长时间内的记录，默认 1000
- `unit`（可选）：时间单位 — `ns`、`us`、`ms`、`s`，默认 `ms`

返回：记录列表，每条包含 `timestamp`、`source`、`content`

### process_write

向进程的 stdin 写入内容。仅进程运行时可用。

- `id`（必填）：内部进程 ID
- `content`（必填）：要写入的字符串

### process_signal

发送操作系统信号。Windows 支持 `CTRL_C_EVENT`（0）、`CTRL_BREAK_EVENT`（1），SIGTERM 映射为 TerminateProcess。POSIX 支持完整信号集（SIGTERM、SIGKILL、SIGINT 等）。

- `id`（必填）：内部进程 ID
- `signal`（必填）：信号名称或编号

### process_list

列出所有托管进程。

返回：`{id, os_pid, status, timeout_ms, inactive_duration_ms, io_count}` 数组

### process_inspect

查看单个进程完整详情。

- `id`（必填）：内部进程 ID

返回：所有元数据，外加 `io_count`、`stdout_count`、`stderr_count`、`stdin_count`

### process_kill

杀死单个进程。输出数据保留，可供后续读取。

- `id`（必填）：内部进程 ID

### process_kill_all

立即杀死所有托管进程。

### process_clear

清空某个进程的所有 I/O 记录。表结构保留，记录被删除。

- `id`（必填）：内部进程 ID

### process_cleanup

移除已终止进程及其全部数据。仅允许对 `exited` 或 `killed` 状态的进程执行。

- `id`（必填）：内部进程 ID

## 虚拟 PTY 支持

默认情况下 Portal 使用系统管道运行程序。检查 `isatty()` 的程序（ssh、
gdb、psql、交互式 REPL）和全屏 TUI（vim、htop、less）需要虚拟 PTY：
给 `process_start` 传 `"pty": true` 即可。Windows 上基于 ConPTY
（Windows 10 1809+），POSIX 上基于 ptyprocess。

PTY 模式与管道模式的差异：
- stdout 与 stderr 合并为单一控制台流（`process_read` 的
  `source="stderr"` 恒为空）
- 记录是任意块，不是行——一行可能跨多条记录，提示符可能没有换行
- 写入的输入会回显到输出流（真实终端行为）——回显是输入，不是输出
- 中断 PTY 进程：用 `process_write` 分两次发送 `\u0003` 和回车（Ctrl+C + Enter，ConPTY 行缓冲，分开发送实测更可靠）；
  `KeyboardInterrupt` 回溯是预期输出
- PTY 进程的 `process_signal` 仅支持 SIGTERM（终止，Windows 上为硬杀）、
  SIGKILL（杀）和 CTRL_C_EVENT（优雅 Ctrl+C）；其他信号会被拒绝
- Windows 上退出码为 `null`（ConPTY 不提供）

`process_screen` 对 PTY 进程做实时屏幕快照——全屏 TUI 的记录流是
乱码片段，请用此工具读取。传 `cols`/`rows` 会先调整实时终端尺寸，
省略则为纯快照。进程退出后屏幕仍可查询，直到 `process_cleanup`。

### 何时使用 `pty`

| 信号 | 例子 | 判定 |
|------|------|------|
| 检查 `isatty()` | ssh、gdb、psql、mysql、telnet、REPL | `pty: true` |
| 全屏 TUI | vim、htop、top、less、man | `pty: true` |
| 交互式 flags | `-i` / `-it` / `-t` | `pty: true` |
| 一次性脚本/批处理 | `python -c`、构建命令 | `pty: false` |
| 分页输出的一次性命令 | git log/diff、less | 管道模式 + `--no-pager`/`GIT_PAGER=cat` |

拿不准时用 `pty: true`——非交互程序容忍 PTY，交互程序没有 PTY 会挂起。

### 程序注册表

`program_query` / `program_record` 维护一个跨会话、机器全局的注册表
（`programs.db`，位于平台应用数据目录，可用 `PORTAL_DATA_DIR` 覆盖），
记录哪些可执行文件需要 PTY。Agent 在首次遇到程序后回写结论（包括
"不需要"的负例）。重复确认递增 `confirmed_count`（>= 2 视为已定案）；
记录相反值会重置计数（视为修正）。

## 进程生命周期

```
START → RUNNING → EXITED  → （只读，数据保留）
                → KILLED  → （只读，数据保留）
                → timeout → KILL + CLEANUP（数据删除）
```

READ 和 WRITE 操作会重置空闲计时器，防止超时被杀死。

## ANSI 过滤

所有颜色码和光标移动序列（`\x1b[...m`、`\x1b[...J` 等）在存储前被过滤移除。管道模式下内容原样存储，保留进程原有的换行格式；PTY 模式下额外的裸回车符（`\r`）也会被移除（ConPTY 输出 `\r\n`）。

## 架构

```
MCP Client
    │ JSON-RPC (stdio)
    ▼
Portal MCP Server
  ├── ProcessManager（进程生命周期 + 超时监控）
  │     └── ManagedProcess（单进程包装器）
  │           └── asyncio.subprocess.Process
  └── Database（SQLite，基于 aiosqlite）
        ├── processes 表（元数据）
        └── proc_<id> 表（I/O 记录，每进程独立）
```

## 开发

```bash
# 同步 uv 环境（包含 pytest）
uv sync

# 运行测试
uv run pytest tests/ -v
```

## License

MIT
