# PlaceTogether Dedicated 24/7 Server Deployment Guide

> 🌐 **[Читать это руководство на русском языке (Russian)](DEDICATED_SERVER_GUIDE_RU.md)**

This guide explains how to deploy the **PlaceTogether 3.2 (SQLite Edition)** server on a remote dedicated server (Linux VPS / VDS or Windows Server), so your collaborative team can work **24/7 autonomously** without depending on any individual player's host PC.

---

## 🌟 How the Autonomous Dedicated Architecture Works

Previously, collaborative building required a designated "Host" to keep Roblox Studio open and send invitations.

In the **PlaceTogether 3.2 Dedicated Edition**:
1. **Robust SQLite 3 Engine (WAL Mode):** Replaces fragile JSON file rewrites with a high-performance SQLite database operating in Write-Ahead Logging mode.
2. **Zero Risk of Data Loss (ACID):** Every block update, part movement, and property change is atomically committed in `< 1ms`. Even in the event of an abrupt power outage, place integrity is preserved.
3. **Automatic Migration:** If you have an existing `data.json` file, the server automatically migrates all rooms and objects into `placetogether.db` on first launch.
4. **24/7 True Persistence:** Projects remain stored on the server permanently. Any team member can connect, build, leave, and return at any time.
5. **Room Password Security:** Access is secured via cryptographic session tokens (Bearer Tokens) and salted SHA-256 hashes with constant-time verification.
6. **Chunked Synchronization:** Large maps are uploaded and downloaded in paginated batches, completely bypassing Roblox `HttpService` payload limits.
7. **Zero External Dependencies:** Built using 100% Python 3 standard library (`sqlite3`, `http.server`, `hashlib`, `socket`).

---

## Option 1. Deploy on Linux with Docker (Recommended)

The fastest and cleanest way to run the server on any Linux VPS (Ubuntu / Debian / CentOS / Alpine) is using the pre-configured Docker setup.

### 1. Copy Files to the Server
Upload the project folder to your VPS (or clone the repository):
```bash
mkdir -p /opt/placetogether
cd /opt/placetogether
```
Ensure the directory contains:
- `server.py`
- `Dockerfile`
- `docker-compose.yml`

### 2. Start the Container
```bash
docker compose up -d --build
```

The server will run in the background and automatically restart on system reboots.
The database is safely persisted in the `./data/placetogether.db` host directory.

View server logs:
```bash
docker compose logs -f
```

Stop the server:
```bash
docker compose down
```

---

## Option 2. Native Linux Deployment via systemd (Without Docker)

If you prefer running directly on the host without Docker:

### 1. Install Python 3 (If Not Already Installed)
```bash
sudo apt update
sudo apt install -y python3
```

### 2. Place Project Files
Create a directory and place `server.py` inside:
```bash
sudo mkdir -p /opt/placetogether
sudo cp server.py /opt/placetogether/
sudo chmod +x /opt/placetogether/server.py
```

### 3. Install the systemd Service
Copy the provided `placetogether.service` file:
```bash
sudo cp placetogether.service /etc/systemd/system/
```
*(If your paths differ, adjust `/etc/systemd/system/placetogether.service`)*.

### 4. Enable and Start the Service
```bash
sudo systemctl daemon-reload
sudo systemctl enable placetogether
sudo systemctl start placetogether
```

Check service status:
```bash
sudo systemctl status placetogether
```

View real-time logs:
```bash
journalctl -u placetogether -f
```

---

## Option 3. Windows Server Deployment

1. Install Python 3.10+ from the official [python.org](https://www.python.org/downloads/) website (ensure **"Add python.exe to PATH"** is checked).
2. Place `server.py` and `StartPlaceTogetherServer.bat` into a folder of your choice (e.g. `C:\PlaceTogether`).
3. Run `StartPlaceTogetherServer.bat` as Administrator (to automatically configure the Windows Firewall inbound rule for port 8787).
4. The server will start and listen on `0.0.0.0:8787`.

*To run as a background service without an open console window, you can use [NSSM (Non-Sucking Service Manager)](https://nssm.cc/) to register `server.py` as a native Windows service.*

---

## Firewall Configuration

The server listens on TCP port **8787**. Make sure this port is allowed in your hosting firewall:

### For Linux (UFW):
```bash
sudo ufw allow 8787/tcp
sudo ufw reload
```

### For Cloud Providers (AWS, Hetzner, DigitalOcean, GCP, Azure, Linode, etc.):
Add an Inbound Security Rule for **TCP port 8787** in your cloud hosting dashboard (Security Groups / Networking Firewall).

---

## (Optional) Reverse Proxy with Custom Domain & HTTPS via Nginx

Roblox Studio natively supports both `http://` and `https://` URLs. If you want to connect a custom domain (e.g., `https://roblox.myteam.com`), configure Nginx with a free Let's Encrypt certificate:

```nginx
server {
    listen 80;
    server_name roblox.myteam.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name roblox.myteam.com;

    ssl_certificate /etc/letsencrypt/live/roblox.myteam.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/roblox.myteam.com/privkey.pem;

    client_max_body_size 50M;

    location / {
        proxy_pass http://127.0.0.1:8787;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header Connection "keep-alive";
        proxy_read_timeout 300s;
    }
}
```

---

## 🎮 How to Collaborate in Roblox Studio

Once your dedicated server is running:

### 1. Install the Updated Plugin
Copy `PlaceTogether.rbxmx` into your Roblox Studio plugins folder (`Plugins -> Plugin Folder`) and restart Studio.

### 2. Configure the Server Address
1. Open the **Settings** tab in the PlaceTogether plugin window.
2. In the **SERVER ADDRESS** field, enter your server URL:
   * Example: `http://198.51.100.25:8787` (or your domain `https://roblox.myteam.com`).
3. The plugin will connect to the server and show status `READY`.

### 3. Create a Project on the Server (First Developer)
1. Open the local place in Roblox Studio that you want to host on the server.
2. Go to the **Projects** tab in the plugin.
3. Under **Create New Project**:
   * Enter a project name (e.g. `MainGame`).
   * Enter an optional password (recommended for privacy).
   * Click **Create & Upload Place**.
4. The plugin creates a new room on the server and uploads your local place state as the initial baseline. The server assigns a unique Room ID (e.g., `rm_3f9a`).

### 4. Connect to the Project (Other Developers)
1. Open Roblox Studio with an **empty Baseplate** (`File -> New`).
2. In the PlaceTogether plugin under the **Projects** tab:
   * In **Server Rooms Browser**, click **Refresh Projects List** (your project will appear).
   * Click **Select Project** (or paste the Room ID manually).
   * If password-protected, enter the password in **ROOM PASSWORD**.
   * Click **Connect to Project**.
3. The plugin securely authenticates, downloads the map, and activates two-way real-time synchronization.

### 5. True Session Autonomy
* Any developer can close Roblox Studio at any time — all changes remain safe in the server's SQLite database.
* When returning later, simply open an empty Baseplate, select the project, and click **Connect to Project** — Studio instantly loads the latest live map state.
