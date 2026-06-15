# Deploying the agent to a Linux VPS (e.g. Hostinger)

Goal: run the agent 24/7 for the contest window so the drawdown breaker can always
fire and no scoring hours are missed. The agent is restart-safe (state persists),
so systemd auto-restart resumes cleanly.

## 0. Prerequisites on the VPS
```bash
python3 --version          # need 3.11+ ; if older, install (e.g. Ubuntu: apt install python3.12 python3.12-venv)
sudo adduser --disabled-password agent   # dedicated non-root user (recommended)
sudo su - agent
```

## 1. Get the code onto the VPS
Option A — git (if you push the repo somewhere private):
```bash
git clone <your-private-repo-url> Track1-trade-onchain
```
Option B — copy from your PC with scp (run on your PC, NOT the VPS):
```powershell
# Excludes secrets and local data; we copy .env separately & securely below.
scp -r E:\Track1-trade-onchain agent@<VPS_IP>:/home/agent/Track1-trade-onchain
```

## 2. Install dependencies
```bash
cd ~/Track1-trade-onchain
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
mkdir -p logs data/runtime
```

## 3. Put secrets on the VPS SECURELY
Do NOT commit or paste the private key into git/chat. Create .env directly on the box:
```bash
nano ~/Track1-trade-onchain/.env       # paste contents from your local .env
chmod 600 ~/Track1-trade-onchain/.env  # owner-only read/write
```
Keep `DRY_RUN=true` for now.

## 4. Smoke-check on the VPS (no money moved)
```bash
cd ~/Track1-trade-onchain
.venv/bin/python -m src.agent status     # shows wallet + equity (confirms RPC/CMC reachable)
.venv/bin/python -m src.agent tick       # one dry-run tick, confirm no errors
```
If `status` can't reach RPC/CMC, check the VPS region/firewall outbound access.

## 5. Install the systemd service
```bash
# edit paths/user in deploy/agent.service if you didn't use /home/agent
sudo cp ~/Track1-trade-onchain/deploy/agent.service /etc/systemd/system/agent.service
sudo systemctl daemon-reload
```

## 6. GO-LIVE (on 22 June, just before the window)
```bash
cd ~/Track1-trade-onchain
.venv/bin/python -m src.agent.registration.register_agent --check   # isRegistered: True
.venv/bin/python -m src.agent status                                # capital + gas present
.venv/bin/python -m src.agent reset                                 # clear stale state (critical)
nano .env                                                           # set DRY_RUN=false
sudo systemctl enable --now agent                                   # start + auto-start on reboot
```

## 7. Monitor
```bash
journalctl -u agent -f                 # live logs
tail -f ~/Track1-trade-onchain/logs/agent.log
systemctl status agent                 # running / restarts
.venv/bin/python -m src.agent status   # equity snapshot anytime
```

## Stop / restart
```bash
sudo systemctl restart agent
sudo systemctl stop agent              # e.g. end of contest (28 June)
```

## Security checklist
- [ ] SSH: key-only auth, password login disabled (`/etc/ssh/sshd_config`: `PasswordAuthentication no`)
- [ ] Firewall: only SSH inbound (`ufw allow OpenSSH && ufw enable`); the agent needs no inbound ports
- [ ] `.env` is `chmod 600`, owned by the `agent` user; never committed
- [ ] Agent runs as non-root `agent` user (not root)
- [ ] If this VPS also hosts public services, understand the key is more exposed — only contest capital sits in the wallet

## Notes
- Live trading needs only BSC RPC + CoinMarketCap (both global). Binance is used
  only for backtesting and is NOT required on the VPS.
- After any crash/reboot, systemd restarts the agent; persisted drawdown peak and
  trade ledger mean it resumes without double-counting or losing breaker state.
