# Prompt Profiles

Prompt profiles control the writing style, language, and workflow the agent follows. Set them with `-p` / `--prompt-profile` or `/profile` in interactive mode.

---

## Available profiles

### `base` (default)

General-purpose profile. Best for software engineering, research, and mixed tasks.

- Full instructions for coding (exploration, testing, uv package manager)
- Research workflow (literature, hypothesis, implementation, analysis, reporting)
- English language

```bash
octoslave -p base
octoslave run "build a REST API" -p base
```

---

### `coder`

Pure coding profile. No research preamble — goes straight to implementation.

- Focused on writing, editing, and debugging code
- English language
- Best for: refactoring, feature implementation, test writing

```bash
octoslave -p coder
octoslave run "add unit tests for all functions in src/" -p coder
```

---

### `analyst`

Data analysis profile. Best for datasets, statistics, and visualisations.

- Focused on data exploration, statistical analysis, plots
- Uses pandas, matplotlib, scipy by default
- English language
- Best for: CSV/JSON data analysis, research data, sales reports

```bash
octoslave -p analyst
octoslave run "analyze sales_data.csv and create summary plots" -p analyst -d ~/data
```

---

## Switching profiles

```bash
# At startup
octoslave -p analyst
octoslave run "task" -p coder

# Mid-session (resets conversation)
/profile analyst
/profile base

# Show current profile and available options
/profile
```

**Note:** Profile changes take effect at the start of the next task. Use `/clear` first if you want to switch mid-session.

---

## Creating a custom profile

Add a `.md` file to `octoslave/prompt_profiles/`:

```bash
cat > octoslave/prompt_profiles/legal.md << 'EOF'
You are OctoSlave — a legal document assistant.

Working directory: {working_dir}
Today: {date}

Write in formal legal English. Cite relevant legislation where applicable.
Structure documents with numbered sections. Flag any ambiguities explicitly.
EOF

octoslave -p legal
```

Available template variables:
- `{working_dir}` — current working directory
- `{date}` — today's date in ISO format
