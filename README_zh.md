# Portal MCP Server

一个专为**交互式程序**设计的 MCP（Model Context Protocol）服务器。通过 MCP 工具调用，实现交互式进程的启动、监控、I/O 读写、信号发送和生命周期管理。

**适用场景：** SSH 远程连接、GDB 调试、数据库 CLI（psql/mysql）、REPL 环境等需要持续交互的程序。

**不适用场景：** 简单的单次命令执行（如 `ls`、`echo`）——这些用 Terminal 自带的 Shell 工具即可。

## 安装

```bash
pip install -e .
```

需要 Python 3.11+。

## 配置

添加到 MCP 客户端配置中（如 Claude Code）：

```json
{
  "mcpServers": {
    "portal": {
      "command": "portal-mcp"
    }
  }
}
```

或者显式指定 Python 路径：

```json
{
  "mcpServers": {
    "portal": {
      "command": "python",
      "args": ["-m", "portal_mcp.server"]
    }
  }
}
```

环境变量 `PORTAL_DB_PATH` 可指定 SQLite 数据库路径（默认：当前目录下的 `portal.db`）。每次 MCP 服务启动时数据库会重新创建。

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

### process_start

启动子进程并开始捕获其输出。

- `command`（必填）：可执行文件或命令
- `args`（可选）：命令行参数列表
- `cwd`（可选）：工作目录
- `env`（可选）：环境变量（与当前环境合并）
- `timeout_ms`（可选）：空闲超时时间（毫秒），0 表示永不超时

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

## 进程生命周期

```
START → RUNNING → EXITED  → （只读，数据保留）
                → KILLED  → （只读，数据保留）
                → timeout → KILL + CLEANUP（数据删除）
```

READ 和 WRITE 操作会重置空闲计时器，防止超时被杀死。

## ANSI 过滤

所有颜色码和光标移动序列（`\x1b[...m`、`\x1b[...J` 等）在存储前被过滤移除。内容原样存储，保留进程原有的换行格式。

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
# 安装开发依赖
pip install -e .
pip install pytest pytest-asyncio

# 运行测试
pytest tests/ -v
```

## License

MIT
