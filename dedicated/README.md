# PlaceTogether - Dedicated 24/7 Server Edition

This directory contains the **Dedicated Server Edition** of PlaceTogether, designed for 24/7 continuous operation on Linux VPS, Windows Server, Docker, or local machines with multi-room persistence and enterprise reliability.

The original lightweight single-room server remains untouched in the root directory.

---

## 🚀 Key Improvements Over Original Server

| Feature | Standard Version (Root) | Dedicated Edition (`dedicated/`) |
| :--- | :--- | :--- |
| **Storage Engine** | Single `data.json` file (rewritten every change) | **SQLite 3 in WAL Mode** (instant ACID commits, zero corruption) |
| **Large Map Support** | Limited by single HTTP payload size | **Paginated synchronization** (handles massive maps safely) |
| **Room Persistence** | Single room in memory/JSON | **Multi-room support**, persistent across server restarts |
| **Security & Passwords** | Plaintext / basic verification | **Salted SHA-256**, constant-time checks, **IP Rate Limiting** |
| **Web Dashboard** | None | **Live real-time dashboard** (Roblox avatars, active editors) |
| **Dashboard Privacy** | N/A | **Togglable** via Studio plugin or `--dashboard on/off` (default: OFF, returns `ERR_EMPTY_RESPONSE`) |
| **Activity Tracking** | Basic status | **Detailed status** (selected object names, script editing, play mode, idle timer) |
| **Deployment** | Windows `.bat` | **Windows `.bat`, Linux systemd service, Docker & Docker Compose** |
| **Console Output** | Verbose HTTP spam | **Clean console**; raw HTTP logs redirected to `server.log` |
| **Backward Compatibility**| N/A | **Automatic migration**: imports existing `data.json` into SQLite on first run |

---

## 📁 Files in This Directory

* **[`server.py`](server.py)** - Dedicated 24/7 multi-threaded HTTP server with SQLite backend. Zero external Python dependencies (standard library only).
* **[`PlaceTogether.rbxmx`](PlaceTogether.rbxmx)** - Enhanced Roblox Studio plugin with project switcher, room passwords, dashboard toggle, and activity reporting.
* **[`StartPlaceTogetherServer.bat`](StartPlaceTogetherServer.bat)** - Instant Windows launcher (< 0.2s startup) with automatic network IP detection.
* **[`Dockerfile`](Dockerfile)** & **[`docker-compose.yml`](docker-compose.yml)** - Production Docker deployment configurations.
* **[`placetogether.service`](placetogether.service)** - Systemd service unit for running 24/7 on Ubuntu/Debian/CentOS Linux VPS.
* **[`DEDICATED_SERVER_GUIDE.md`](DEDICATED_SERVER_GUIDE.md)** - Step-by-step installation, VPS setup, firewall, and tunnel guide.

---

## 🏁 Quick Start

### Windows
Double-click **`StartPlaceTogetherServer.bat`**.
The server will start instantly and display the IP address to enter into the Roblox Studio plugin.

### Linux VPS / Ubuntu
```bash
cd dedicated
python3 server.py --port 8787
```

### Docker
```bash
cd dedicated
docker compose up -d
```

---

## ⚙️ Command-Line Arguments

```text
usage: server.py [-h] [--port PORT] [--host HOST] [--db DB_FILE] [--log LOG_FILE] [--dashboard {on,off,auto}]

options:
  --port PORT                   Port to listen on (default: 8787)
  --host HOST                   Host/IP to bind to (default: 0.0.0.0)
  --db DB_FILE                  Path to SQLite database (default: placetogether.db)
  --log LOG_FILE                Path to HTTP access log (default: server.log)
  --dashboard {on,off,auto}     Web Dashboard state (default: auto, reads SQLite setting)
```

For detailed guides on remote VPS setup, systemd autostart, and tunnels, see [DEDICATED_SERVER_GUIDE.md](DEDICATED_SERVER_GUIDE.md).
