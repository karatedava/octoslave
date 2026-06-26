# Vault Improve — Autonomous Note Enhancement Pipeline

OctoSlave can autonomously improve every Markdown note in an Obsidian vault (or any folder of `.md` files) without supervision. It runs in batches, saves progress after each one, and can be resumed at any time.

---

## How it works

```
Planning → [Editor → Verifier → Editor-fix] × N batches → Reporter
```

1. **Planner** — scans all `.md` files, groups them by folder into batches of max 8 files, writes `plan.json`
2. **Editor** (`deepseek-v3.2`) — rewrites each batch: improves structure, adds `[[wikilinks]]`, fills thin sections, adds cross-references
3. **Verifier** (`deepseek-v3.2-thinking`) — fact-checks via web search, writes `.vault_work/<batch>_issues.md` with confirmed errors
4. **Editor-fix** — patches only the confirmed issues, leaves everything else untouched
5. **Reporter** — writes `.vault_work/report.md` with statistics: notes changed, issues fixed, wikilinks added

Progress is saved after every batch to `.vault_work/plan.json`. Ctrl+C resets the current batch to `pending` so `--resume` retries it cleanly.

---

## Usage

### As a CLI command (headless / systemd)

```bash
octoslave vault-improve VAULT_PATH [OPTIONS]
```

| Flag | Short | Description |
|------|-------|-------------|
| `VAULT_PATH` | | Path to vault (default: current directory) |
| `--profile NAME` | `-p` | Writing style profile (`base`, `coder`, etc.) |
| `--model MODEL` | `-m` | Override model for all agents |
| `--resume` | | Skip completed batches, continue from last position |
| `--api-key KEY` | | API key |
| `--base-url URL` | | API base URL |

```bash
# Basic run
octoslave vault-improve ~/Brain2 --profile base

# Resume after crash or interruption
octoslave vault-improve ~/Brain2 --profile base --resume

# Use a stronger model for better quality
octoslave vault-improve ~/Brain2 --model deepseek-v3.2-thinking

# Run in background, log to file
nohup octoslave vault-improve ~/Brain2 --profile base --resume \
  > ~/octoslave/vault.log 2>&1 &
```

### As a slash command (interactive mode)

```
/vault-improve [PATH] [--model MODEL] [--resume]
```

```bash
/vault-improve ~/Brain2 --profile base    # profile from /profile command
/vault-improve ~/Brain2 --resume
/vault-improve                                 # uses current working directory
```

The active `/profile` is automatically used — no need to pass it separately in the slash command.

---

## Running 24/7 on a server (systemd)

```ini
# /etc/systemd/system/octoslave-vault.service
[Unit]
Description=OctoSlave Vault Improve
After=network.target

[Service]
Type=simple
User=kowalski
WorkingDirectory=/home/kowalski/octoslave
ExecStart=/home/kowalski/octoslave/.venv/bin/octoslave vault-improve /home/kowalski/Brain2 --profile base --resume
Restart=on-failure
RestartSec=30
StandardOutput=append:/home/kowalski/octoslave/vault.log
StandardError=append:/home/kowalski/octoslave/vault.log

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now octoslave-vault
tail -f ~/octoslave/vault.log          # watch live
tail -n 10 ~/octoslave/vault.log       # last 10 lines
sudo systemctl status octoslave-vault  # check if running
sudo systemctl stop octoslave-vault    # stop
```

---

## Vault sync with Obsidian (Syncthing)

To run vault-improve on a server while Obsidian stays on your Mac:

```
Mac (~/Documents/Brain2) ←──syncthing──→ Server (/home/kowalski/Brain2)
```

OctoSlave writes to `/home/kowalski/Brain2` on the server → Syncthing syncs changes to your Mac within seconds → Obsidian sees the updated notes automatically.

Install Syncthing:
```bash
# Mac
brew install syncthing

# Debian/Ubuntu server
apt install syncthing

# Pair via web UI at http://localhost:8384
```

---

## Output files

All pipeline working files are written to `VAULT_PATH/.vault_work/`:

| File | Description |
|------|-------------|
| `plan.json` | Batch plan with status for each batch (`pending` / `done`) |
| `<batch_id>_issues.md` | Verifier's confirmed errors for that batch |
| `report.md` | Final report: stats, changes, wikilinks created |

---

## Resuming after a crash

```bash
# Check which batches completed
cat ~/Brain2/.vault_work/plan.json | python3 -m json.tool | grep -E '"status"|"label"'

# Resume — skips done batches, retries pending ones
octoslave vault-improve ~/Brain2 --profile base --resume
```

---

## Profiles with vault-improve

Pass any prompt profile with `--profile NAME` to control the writing style the
editor applies to your notes:

```bash
octoslave vault-improve ~/Brain2 --profile base
```

See [PROMPT_PROFILES.md](PROMPT_PROFILES.md) for all profiles.
