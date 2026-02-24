# AWS Lightsail Deployment Guide

Step-by-step guide to deploy the RSI Options Trading Strategy on AWS Lightsail.

## 1. Create a Lightsail Instance

1. Go to [AWS Lightsail Console](https://lightsail.aws.amazon.com)
2. Click **Create instance**
3. Settings:
   - **Region**: Mumbai (ap-south-1) for lowest latency to Indian exchanges
   - **Platform**: Linux/Unix
   - **Blueprint**: Ubuntu 22.04 LTS
   - **Plan**: $5/month (1 GB RAM, 1 vCPU) — enough for 1-2 forward tests
   - For 3+ simultaneous tests: use $10/month (2 GB RAM)
4. Give it a name (e.g., `rsi-trading-bot`)
5. Click **Create instance**

## 2. Connect via SSH

```bash
# Download the default key from Lightsail console
ssh -i ~/.ssh/LightsailDefaultKey.pem ubuntu@<your-instance-ip>
```

## 3. Install Docker

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker ubuntu

# Install Docker Compose
sudo apt install -y docker-compose-plugin

# Log out and back in for group changes
exit
```

SSH back in, then verify:

```bash
docker --version
docker compose version
```

## 4. Clone and Configure

```bash
# Clone the repo
git clone https://github.com/your-repo/RSI-Options-Trading-Strategy.git
cd RSI-Options-Trading-Strategy

# Create .env from template
cp .env.example .env

# Edit with your Dhan credentials
nano .env
```

Set these values in `.env`:

```
CLIENT_ID=your_actual_client_id
ACCESS_TOKEN=your_actual_access_token
TRADING_MODE=paper
FORWARD_STRATEGY=rsi_ema_cross
FORWARD_INSTRUMENT=NIFTY
```

## 5. Start the Services

```bash
# Build and start (detached mode)
docker compose up -d --build

# Check status
docker compose ps

# Watch forward test logs
docker compose logs -f forward

# Watch UI logs
docker compose logs -f ui
```

The Streamlit UI will be available at `http://<your-instance-ip>:8501`.

## 6. Open Port 8501

In Lightsail console:
1. Go to your instance → **Networking**
2. Under **IPv4 Firewall**, click **Add rule**
3. Application: **Custom**, Protocol: **TCP**, Port: **8501**
4. Click **Create**

## 7. Running Multiple Strategies

To run multiple forward tests simultaneously, create a second forward service:

```bash
# Run SENSEX strategy alongside NIFTY
docker compose run -d --name rsi-forward-sensex \
  -e FORWARD_STRATEGY=rsi_ema_cross \
  -e FORWARD_INSTRUMENT=SENSEX \
  forward
```

Or add it to `docker-compose.yml`:

```yaml
  forward-sensex:
    build: .
    container_name: rsi-forward-sensex
    command: >
      python forward_test_runner.py
      --strategy rsi_ema_cross
      --instrument SENSEX
    env_file:
      - .env
    volumes:
      - ./saved_strategies:/app/saved_strategies
      - ./forward_test_logs:/app/forward_test_logs
    restart: unless-stopped
```

## 8. Switching to Live Trading

1. Edit `.env`: set `TRADING_MODE=live`
2. Restart: `docker compose restart forward`
3. Monitor closely: `docker compose logs -f forward`

**Safety**: The system has a kill switch and 35% daily loss limit.
Always test in paper mode first.

## 9. Maintenance

```bash
# Update code
cd RSI-Options-Trading-Strategy
git pull
docker compose up -d --build

# View logs
docker compose logs --tail=100 forward

# Restart after crash
docker compose restart forward

# Stop everything
docker compose down
```

## 10. Access Token Renewal

Dhan access tokens expire periodically. When yours expires:

1. Generate a new token on [Dhan](https://dhan.co)
2. Update `.env` with the new `ACCESS_TOKEN`
3. Restart: `docker compose restart`

Consider automating this with the `DhanLogin` class (see `dhanhq` docs).
