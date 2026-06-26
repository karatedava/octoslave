# Deployment — Running OctoSlave on a Server

OctoSlave is a standard Python CLI and runs fine headlessly on any Linux server (Proxmox LXC, VPS, Raspberry Pi, etc.).

---

## Install on server

```bash
# Install system deps
apt update && apt install -y python3 python3-pip python3-venv git

# Clone repo
git clone https://github.com/karatedava/octoslave ~/octoslave
cd ~/octoslave

# Create venv and install
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Configure
octoslave config
```

If you get a permission error on `pip install -e .`:
```bash
sudo chown -R $USER:$USER ~/octoslave
pip install -e .
```

---

## Updating

```bash
cd ~/octoslave
git pull
source .venv/bin/activate
pip install -e .
```

---

## Running as a systemd service

### Vault improve (24/7 unattended)

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
sudo systemctl status octoslave-vault
tail -f ~/octoslave/vault.log
tail -n 10 ~/octoslave/vault.log
```

### Any one-shot task

```ini
# /etc/systemd/system/octoslave-research.service
[Unit]
Description=OctoSlave Research Task
After=network.target

[Service]
Type=simple
User=kowalski
WorkingDirectory=/home/kowalski/octoslave
ExecStart=/home/kowalski/octoslave/.venv/bin/octoslave run "YOUR TASK HERE" --dir /home/kowalski/projects/mytask
Restart=no
StandardOutput=append:/home/kowalski/octoslave/research.log
StandardError=append:/home/kowalski/octoslave/research.log

[Install]
WantedBy=multi-user.target
```

### Web UI as a service

```ini
# /etc/systemd/system/octoslave-web.service
[Unit]
Description=OctoSlave Web UI
After=network.target

[Service]
Type=simple
User=kowalski
WorkingDirectory=/home/kowalski/octoslave
ExecStart=/home/kowalski/octoslave/.venv/bin/octoslave web --host 0.0.0.0 --port 7860 --no-browser
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

---

## Remote access from phone or outside LAN

### Tailscale (recommended — works anywhere)

```bash
# On server
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up

# On phone: install Tailscale app
# Access: http://<tailscale-ip>:7860
```

### SSH access

```bash
ssh kowalski@server-ip
```

Use `screen` to keep sessions alive after disconnecting:

```bash
screen -S mysession      # create named session
# ... do stuff ...
# Ctrl+A, D             # detach
screen -r mysession      # reattach later
screen -ls               # list all sessions
```

---

## Vault sync with Obsidian (Syncthing)

Keep your Obsidian vault in sync between Mac and server automatically:

```
Mac (~/Documents/Brain2) ←──syncthing──→ Server (/home/kowalski/Brain2)
```

```bash
# Mac
brew install syncthing
brew services start syncthing
# Open http://localhost:8384 to configure

# Server (Debian/Ubuntu)
apt install syncthing
systemctl --user enable --now syncthing
# Open http://localhost:8384 to pair with Mac
```

OctoSlave writes to `/home/kowalski/Brain2` → Syncthing syncs to Mac → Obsidian sees updated notes within seconds. Mac can be off while OctoSlave works; notes appear when you turn it back on.

---

## Checking what's running

```bash
# List OctoSlave processes
ps aux | grep octoslave

# Check systemd service status
sudo systemctl status octoslave-vault

# Watch logs live
tail -f ~/octoslave/vault.log

# Last 10 log lines
tail -n 10 ~/octoslave/vault.log
```
