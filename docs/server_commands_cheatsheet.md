# Server Commands Cheatsheet

Quick reference for managing the algo engine on Lightsail (Ubuntu + Docker).

---

## 1. Connect to Server

**From browser:** Open [Lightsail console](https://lightsail.aws.amazon.com) → click your instance → click **"Connect using SSH"**

**From Mac terminal:**

```bash
ssh -i ~/.ssh/LightsailDefaultKey-ap-south-1.pem ubuntu@<your-lightsail-ip>
```

---

## 2. Ubuntu / Linux Basics

| Command | What it does |
|---|---|
| `ls` | List files in current directory |
| `ls -la` | List all files with details (including hidden ones like `.env`) |
| `cd ~/backtest-engine` | Go to your project folder |
| `cd ..` | Go up one directory |
| `pwd` | Print where you are right now (current directory path) |
| `cat .env` | Display contents of a file |
| `nano .env` | Edit a file (Ctrl+O = save, Ctrl+X = exit) |
| `cp .env .env.backup` | Make a backup copy of a file |
| `df -h` | Check disk space |
| `free -h` | Check RAM usage |
| `top` | Live view of CPU/memory usage (press `q` to quit) |

---

## 3. Git

| Command | What it does |
|---|---|
| `git pull` | Download latest code from GitHub |
| `git status` | See what files changed |
| `git log --oneline -5` | See last 5 commits |

---

## 4. Docker Compose

| Command | What it does |
|---|---|
| `docker compose up -d` | Start all containers in background |
| `docker compose up -d --build` | Rebuild image + start (use after code changes) |
| `docker compose down` | Stop and remove all containers |
| `docker compose restart` | Restart all containers (without rebuilding) |
| `docker compose ps` | Show running containers and their status |
| `docker compose logs -f` | Follow live logs from all containers (Ctrl+C to stop) |
| `docker compose logs -f forward` | Follow logs from forward test only |
| `docker compose logs -f ui` | Follow logs from Streamlit UI only |
| `docker compose logs --tail 50` | Show last 50 lines of logs |

The `-d` flag means "detached" — runs in the background so you can close
the terminal without killing the containers.

---

## 5. Common Workflows

### Deploy new code

```bash
cd ~/backtest-engine && git pull && docker compose down && docker compose up -d --build
```

### Change env variables (API token, strategy, etc.)

```bash
cd ~/backtest-engine
nano .env
# Make your edits, then Ctrl+O to save, Ctrl+X to exit
docker compose down && docker compose up -d
```

No `--build` needed for env changes — containers pick up new values on restart.

### Check if things are running

```bash
docker compose ps
```

### Something broken? Check logs

```bash
docker compose logs --tail 100
```

### Restart without any changes

```bash
docker compose restart
```

---

## 6. Download Logs / CSV Files

Logs and CSVs are saved to `~/backtest-engine/forward_test_logs/` on the server
(mounted volume from Docker — files persist even if containers restart).

### List available files

```bash
ls -la ~/backtest-engine/forward_test_logs/
```

### View a file on the server

```bash
cat ~/backtest-engine/forward_test_logs/trades_NIFTY.csv
```

### Download to your Mac

Run this **from your Mac terminal** (not the server):

```bash
scp -i ~/.ssh/LightsailDefaultKey-ap-south-1.pem \
  ubuntu@<your-lightsail-ip>:~/backtest-engine/forward_test_logs/trades_NIFTY.csv \
  ~/Desktop/
```

### Download all logs at once

```bash
scp -i ~/.ssh/LightsailDefaultKey-ap-south-1.pem -r \
  ubuntu@<your-lightsail-ip>:~/backtest-engine/forward_test_logs/ \
  ~/Desktop/forward_test_logs/
```

Replace `<your-lightsail-ip>` with your Lightsail instance's public IP.

### Key paths on the server

| Path | Contents |
|---|---|
| `~/backtest-engine/forward_test_logs/` | Forward test logs and trade CSVs |
| `~/backtest-engine/saved_strategies/` | Strategy JSON configs |
| `~/backtest-engine/data/` | Parquet data files |
| `~/backtest-engine/.env` | Environment variables (API keys, config) |
