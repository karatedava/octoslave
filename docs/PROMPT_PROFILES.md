# Prompt Profiles

OctoSlave now supports multiple prompt profiles that control how much guidance the agent receives about its behavior.

## Available Profiles

### `base` (default)
The full prompt with detailed instructions for:
- Software engineering tasks (exploration, testing, uv package manager)
- Research & scientific tasks (data discovery, literature survey, hypothesis design, implementation, analysis, iteration, reporting)
- Important notes about tool usage and context management

Use this profile when you want the agent to follow established best practices for coding and research tasks.

### `simple`
A minimal prompt that only includes:
- Basic agent identity
- Available tools list

Use this profile when you want maximum flexibility to specify custom behavior in your task description. The agent will still have access to all tools but won't follow predefined workflows.

### `strict`
A conservative profile that requires explicit user confirmation before any file modifications or potentially destructive operations. Includes:
- Basic agent identity
- Available tools list
- **CRITICAL RULE**: Must ask for confirmation before using `write_file`, `edit_file`, or any `bash` command that could change the project state
- Clear workflow: read → analyze → present plan → wait for approval → execute

Use this profile when working with critical codebases where accidental changes could be costly, or when you want full control over what changes are made.

## Usage

### Command Line

```bash
# Use default base profile
ots run "build a REST API"

# Use simple profile for custom behavior
ots run "do whatever makes sense for this data" -p simple

# Use strict profile to confirm before editing
ots run "fix the bug in main.py" -p strict

# Or with full option name
ots run "analyze this dataset" --prompt-profile simple
```

### Interactive Mode

```bash
# Start session
ots

# Check current profile
/profile

# Switch to simple profile
/profile simple

# Switch to strict profile
/profile strict

# Switch back to base profile
/profile base

# List available profiles
/profile
```

## How It Works

When you start a new task (or clear the conversation), the system prompt is loaded from the selected profile file in `octoslave/prompt_profiles/`. The profile is automatically populated with:
- Current working directory
- Today's date

**Note:** Profile changes only affect new conversations. If you want to switch profiles mid-session, use `/clear` first to reset the conversation.

## Adding Custom Profiles

You can create additional profiles by adding `.md` files to the `octoslave/prompt_profiles/` directory:

```bash
# Create a custom profile
cat > octoslave/prompt_profiles/coding.md << 'EOF'
"""\
You are OctoSlave — a focused coding assistant.

Working directory: {working_dir}
Today: {date}

## Tools available

[... your custom instructions ...]
"""
EOF

# Use it
ots run "refactor this code" -p coding
```

## Profile Format

Profile files should be Markdown with optional Python string formatting placeholders:
- `{working_dir}` - Current working directory
- `{date}` - Today's date in ISO format

Files can optionally be wrapped in triple quotes (`"""`) for compatibility with Python string literals.
