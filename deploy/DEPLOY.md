# VPS Deployment

Assumes: Debian/Ubuntu VPS, Caddy already installed and running, a subdomain
(e.g. `trains.your-domain.com`) pointed at the VPS IP.

---

## 1. Check Python version

```bash
python3 --version   # needs 3.11+
```

If below 3.11, on Ubuntu/Debian:
```bash
sudo apt update && sudo apt install -y python3.12 python3.12-venv
```

---

## 2. Create a dedicated user

```bash
sudo useradd --system --create-home --shell /bin/bash latetrain
```

---

## 3. Clone the repo

```bash
sudo mkdir -p /opt/my-late-train
sudo git clone https://github.com/SuhailHussain/my-late-train.git /opt/my-late-train
sudo chown -R latetrain:latetrain /opt/my-late-train
```

---

## 4. Create the virtualenv and install

```bash
sudo -u latetrain python3 -m venv /opt/my-late-train/.venv
sudo -u latetrain /opt/my-late-train/.venv/bin/pip install -e /opt/my-late-train
```

---

## 5. Create the data directories

```bash
sudo -u latetrain mkdir -p /opt/my-late-train/data/logs
```

---

## 6. Create the .env file

```bash
sudo -u latetrain nano /opt/my-late-train/.env
```

Paste (replace all values):

```env
RTT_REFRESH_TOKEN=your_rtt_token_here
HSP_API_KEY=your_hsp_key_here
```

---

## 7. Install the systemd service

```bash
sudo cp /opt/my-late-train/deploy/late-train.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable late-train
sudo systemctl start late-train

# Verify it started
sudo systemctl status late-train
sudo journalctl -u late-train -n 30
```

---

## 8. Add the Caddy block

Edit your existing Caddyfile (usually `/etc/caddy/Caddyfile`):

```bash
sudo nano /etc/caddy/Caddyfile
```

Add the contents of `deploy/Caddyfile`, replacing `trains.your-domain.com`
with your actual subdomain. Keep your existing site blocks — just append this one.

Then reload Caddy:

```bash
sudo systemctl reload caddy
```

Caddy will automatically obtain a TLS certificate from Let's Encrypt.

---

## Updating after a code change

```bash
cd /opt/my-late-train
sudo -u latetrain git pull
sudo -u latetrain /opt/my-late-train/.venv/bin/pip install -e .
sudo systemctl restart late-train
```

---

## Useful commands

```bash
# View live logs
sudo journalctl -u late-train -f

# Check gunicorn access log
sudo tail -f /opt/my-late-train/data/logs/access.log

# Restart
sudo systemctl restart late-train
```
