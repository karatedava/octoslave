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

### `cryouncle`

Cryo-EM companion for structural biologists. Connects directly to a
[CryoSPARC](https://cryosparc.com/) instance and acts as a hands-on
bioinformatician driving the single-particle workflow toward the best possible
3D protein structure.

- Browses projects / workspaces / jobs, inspects result datasets (particle
  counts, CTF fit, defocus, pose distributions), reads job logs.
- Creates and queues processing jobs (motion correction, CTF, picking, 2D/3D
  classification, ab-initio, refinements), downloads maps for local analysis.
- Diagnoses stalled/low-resolution refinements and proposes concrete next
  experiments.
- **Interactive first-run setup:** the agent asks for the CryoSPARC connection
  details (license ID, host, base port, email, password), stores them locally
  (`~/.octoslave/cryosparc.json`, chmod 600), verifies the connection, and does
  all backend wiring — you only answer a few questions once.
- Works identically in the terminal and the web UI.

Requires the CryoSPARC Python client (installed on demand):

```bash
pip install cryosparc-tools        # or: octoslave install ".[cryo]"
octoslave -p cryouncle
octoslave run "connect to my CryoSPARC and suggest how to push J42 past 3 Å" -p cryouncle
```

The CryoSPARC toolbox (`cryo_connect`, `cryo_status`, `cryo_projects`,
`cryo_workspaces`, `cryo_jobs`, `cryo_job`, `cryo_job_types`, `cryo_dataset`,
`cryo_create_job`, `cryo_queue_job`, `cryo_control_job`, `cryo_download`) is
exposed **only** under this profile, so other profiles aren't handed cryo-EM
schemas. Jobs are *built* (not run) by default; queueing compute asks for
confirmation unless you run autonomously.

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
