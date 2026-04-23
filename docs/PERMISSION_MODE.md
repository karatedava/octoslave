# Permission Mode Feature

OctoSlave now supports two permission modes that control how the agent interacts with your filesystem and executes commands.

## Permission Modes

### Autonomous Mode (Default)
- **Behavior**: OctoSlave works without asking for permission
- **Use case**: When you trust the agent to make changes freely
- **Best for**: Automated tasks, trusted workflows, rapid prototyping

### Controlled Mode
- **Behavior**: OctoSlave asks for permission before making any changes
- **Use case**: When you want oversight over file edits and command execution
- **Best for**: Production code, sensitive projects, learning/exploration

## Tools Requiring Permission (in Controlled Mode)

The following tools will trigger a permission request in controlled mode:
- `write_file` - Create or overwrite files
- `edit_file` - Modify existing files
- `bash` - Execute shell commands

Read-only tools (`read_file`, `glob`, `grep`, `list_dir`, `web_search`, `web_fetch`) never require permission.

## How to Use

### Command Line

```bash
# Start with controlled mode
ots --permission-mode controlled

# Run a task with controlled mode
ots run "edit my code" --permission-mode controlled

# Run with autonomous mode (default)
ots run "build something" --permission-mode autonomous
```

### Interactive Mode

Once in the interactive session, use the `/permission` command:

```
/permission              # Show current mode
/permission controlled   # Switch to controlled mode
/permission autonomous   # Switch to autonomous mode
```

### Configuration

You can set the default permission mode permanently:

```bash
# Using the config command
ots config

# Or set via environment variable
export OCTOSLAVE_PERMISSION_MODE=controlled
```

## Permission Prompt

When in controlled mode, OctoSlave will display a prompt like this before executing a modifying tool:

```
┌────── Controlled Mode ──────┐
│  ⚠ Permission Required      │
│                             │
│  ✏️  write_file             │
│                             │
│  OctoSlave wants to:        │
│  create/overwrite file:     │
│  src/main.py                │
│                             │
│  Working directory: /path   │
└─────────────────────────────┘

Allow? (y)/n 
```

Press `y` (or `yes`, `ok`, `allow`) to permit the action, or `n` (or `no`, `deny`) to block it.

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `OCTOSLAVE_PERMISSION_MODE` | Set permission mode | `autonomous` |

## Examples

### Example 1: Safe Code Exploration
```bash
# Start in controlled mode to safely explore unfamiliar codebase
ots --permission-mode controlled

# Agent can read files freely, but will ask before any edits
```

### Example 2: Automated Refactoring
```bash
# Use autonomous mode for trusted automated tasks
ots run "refactor all test files to use pytest" --permission-mode autonomous
```

### Example 3: Mixed Workflow
```bash
# Start interactive session
ots

# Begin in autonomous mode for exploration
# Switch to controlled when ready to make changes
/permission controlled
```

## Implementation Details

- Permission mode is checked at tool execution time in `tools.py`
- The `display.request_permission()` function handles user interaction
- Mode is stored in `~/.octoslave/config.json`
- Environment variables override config file settings
