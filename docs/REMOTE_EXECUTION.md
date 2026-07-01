# Remote execution (SSH)

> Run the agent's tools on a remote machine over SSH — bash and file operations
> execute on the host you choose, not your laptop.

By default OctoSlave operates on your **local** machine. Switch to a configured
**remote host** and the agent's filesystem and shell tools run *there* instead:

- `bash`, `run_background` / `check_process` / `stop_process`
- `read_file`, `write_file`, `edit_file`, `apply_patch`
- `glob`, `grep`, `list_dir`

This is for remote compute, GPUs, or data that lives on a server. **Local is
always the default** — you opt in per session and switching back is instant.

Path-based **bio/OCR tools** still work in remote mode: the remote file is staged
to a local temp copy, processed with your local libraries (pandas, rdkit,
pymupdf…), and any output pushed back. Network tools (`web_search`, `web_fetch`,
`uniprot_lookup`, …) always run locally. **MCP servers stay local.**

---

## How it connects

OctoSlave shells out to your **system `ssh`/`scp`**, so it inherits everything
your SSH already knows:

- `~/.ssh/config` (host aliases, `HostName`, `User`, `ProxyJump`, …)
- your keys and **ssh-agent**
- known-hosts / host-key checking

It opens **one multiplexed connection** per host (OpenSSH `ControlMaster`) and
reuses it for every tool call, so file ops and commands are fast.

### Authentication

Auth is **key/agent based** — OctoSlave runs SSH with `BatchMode=yes` so a
misconfigured host fails fast instead of hanging on a password prompt. That means:

- The host must accept one of your keys (public key in its `authorized_keys`).
- A **passphrase-protected key must be loaded into ssh-agent once**:

  ```bash
  ssh-add ~/.ssh/id_ed25519            # prompts for the passphrase
  # macOS: persist it in the keychain
  ssh-add --apple-use-keychain ~/.ssh/id_ed25519
  ```

Interactive password logins are not supported (by design — they'd stall an
autonomous agent).

---

## Terminal (TUI / CLI)

### Register a host

```bash
ots
  /remote add
```

You'll be asked for: **id** (a short handle), **name**, **host**, **SSH user**
(blank = your ssh default), **port** (default 22), and an optional **identity
file**. You are *not* asked for a working directory — you pick that after
connecting (see below).

### Switch between local and remotes

```bash
/remote                 # show the current target + a reachability check
/remote list            # list every configured remote
/remote <id>            # connect to one over SSH (starts in its home directory)
/remote local           # back to local execution (the default)
/remote remove <id>     # delete a remote
```

When you connect, the working directory becomes the remote **home** directory.
Change it with `/dir <path>` (in remote mode this takes a path *on the remote
host*; no local validation is done).

### One-shot runs

```bash
ots run "train the model" --remote gpu01
ots run "profile the dataset" --remote gpu01 -d /data/experiments
ots improved run "refactor the pipeline" --remote gpu01     # council mode too
```

`--remote <id>` works on `ots`, `ots run`, and `ots improved`. With `--remote`
the `-d` path is treated as a **remote** path (it is not resolved against your
local filesystem).

The prompt/toolbar shows a `🌐 ⇅<name>` marker while a remote is active.

---

## Web UI

Next to the working-directory picker there is a segmented control:

```
💻 Local   |   🌐 Remote        [ gpu01 ▾ ]   ⚙
```

- **Local** — the default; tools run on the machine hosting the web server.
- **Remote** — routes tools over SSH. When more than one host is configured, the
  dropdown lets you choose which one.
- **⚙** — opens the **Remote hosts (SSH)** card in Settings.

The toggle appears on the start screen and after every **New Chat**.

**No remote configured yet?** Clicking **Remote** takes you straight to the
**Remote hosts (SSH)** card in *Settings*, where you can **Add**, **Test
connection**, and **Delete** hosts. The form asks only for connection details —
no working directory.

**Choosing the folder.** After you connect, you start in the remote home
directory. The **Browse…** button next to the working-directory field then opens
a picker that **navigates folders on the remote host** — click into sub-folders,
use `..` to go up, and *Use this folder* to select it.

---

## Where it's stored

Remotes live under a `remotes` key in `~/.octoslave/config.json`, e.g.:

```json
{
  "remotes": [
    {
      "id": "gpu01",
      "name": "GPU box",
      "host": "gpu.example.org",
      "user": "me",
      "port": 22,
      "remote_dir": "",
      "identity_file": ""
    }
  ]
}
```

`remote_dir` is optional — empty means "start in the remote home directory".
`identity_file` is optional too (SSH will pick the right key from your agent /
config otherwise).

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `Permission denied (publickey)` | Your key isn't authorized on the host, or the right key isn't in ssh-agent. `ssh-add` it, or set `identity_file`. Verify with a plain `ssh <host>`. |
| Connection just "fails" with a passphrase key | `BatchMode=yes` won't prompt — load the key: `ssh-add ~/.ssh/id_...`. |
| Host needs a **password** (no keys) | Not supported. Set up key auth (`ssh-copy-id <host>`). |
| Uses the wrong username | Add a `Host` block to `~/.ssh/config` with `User`, or set the user when adding the remote. |
| Jump host / bastion | Put a `ProxyJump` in `~/.ssh/config` — OctoSlave uses your system ssh, so it just works. |
| Slow first call | The first tool call opens the master connection; subsequent calls reuse it. |

---

## Notes & limits

- Remote **background jobs** are launched detached (`nohup`) and intentionally
  survive the SSH channel; stop them with `stop_process`.
- Very large remote files are previewed with a remote `head` rather than being
  transferred whole.
- Bio/OCR tools that *write nested* output paths push the top-level produced
  files back to the working dir; deeply nested output layouts may not round-trip
  in this first version.
- MCP filesystem servers are not re-pointed at the remote — MCP runs locally.
