# Place Together (Dedicated 24/7 Co-op Edition)

> 📌 **Fork Notice / Примечание о форке:**  
> This repository is an enhanced fork of the original [Place Together](https://github.com/Gre4ka43/Place-Together) by [@simonplay43](https://github.com/Gre4ka43) and [@MNKPO57](https://github.com/MNKPO57).  
> It is based on the proven, stable **v0.1.0 architecture** rather than the experimental v0.1.1 rewrite. This edition is engineered for continuous 24/7 hosting, multi-project persistence, zero data loss, and rock-solid collaboration without rig/element distortions.

---

## ⚡ What Makes This Fork Different from Original v0.1.0?

| Feature | Original Place Together v0.1.0 | This Dedicated Fork |
| :--- | :--- | :--- |
| **Storage Engine** | Single `data.json` rewritten on every edit | **SQLite 3 (WAL Mode)**: instant ACID transactions, zero data corruption, handles massive maps |
| **Room Persistence & Projects** | Single room in volatile RAM/JSON | **Multi-Project Browser**: persistent rooms across server restarts, create/switch/delete projects |
| **Security & Passwords** | Open rooms / no password protection | **Salted SHA-256 room passwords**: protect private projects with individual passwords |
| **Rig & Eclipse Animations** | Accessories and limbs collapse into torso when moving rigs | **Relative Weld Math & AccessoryWeld preservation**: full compatibility with custom character rigs and Eclipse Animation Editor |
| **Developer AFK / Idle Tracking** | No idle detection | **Live AFK Timer**: displays `IDLE (1m 25s)` in status badge, overhead 3D billboards, and peer cards |
| **Web Dashboard** | None | **Live real-time Web Dashboard (`http://localhost:8787/`)**: view server status, project stats, and active peers with Roblox avatars |
| **Sync Engine & CPU Load** | Periodic idle scans consuming CPU | **Event-Driven 0% CPU Sync**: completely idle when no changes occur; no continuous background voxel scans |
| **Force Push / Pull Performance** | 100-object fixed batches without progress | **Dynamic High-Speed Batching (up to 400 obj/batch)** with real-time percentage progress bar |
| **Deployment Options** | Windows `.bat` only | **Windows `.bat`, Linux VPS systemd service, Docker & Docker Compose** |
| **Console & Logs** | Verbose HTTP request spam | **Clean informative console**; raw HTTP traffic neatly redirected to `server.log` |
| **Server Shutdown** | Cmd batch wrapper prompts `Terminate batch job (Y/N)?` | **Direct Python launcher**: instant clean Ctrl+C shutdown with zero prompt delays |

---

## 🚀 Key Features

### 1. 🛡️ SQLite 3 WAL Storage (Zero Data Loss)
Unlike JSON-based storage that rewrites the entire file upon every change (risking corruption on large maps or abrupt shutdowns), this fork stores objects in an ACID-compliant SQLite 3 database with Write-Ahead Logging (`WAL` mode). 
- Instant incremental writes.
- Safe 24/7 hosting for heavy projects (tested with 10,000+ objects).
- Auto-migrates legacy `data.json` to SQLite on first startup if detected.

### 2. 🎭 Full Rig & Eclipse Animation Support
The original sync engine recalculated welded parts incorrectly during live movement, causing hats, accessories, and animated limbs to snap into the character's torso. 
This fork implements relative transformation math and preserves `AccessoryWeld` / `Motor6D` matrices, ensuring custom rigs and **Eclipse Animation Editor** projects stay completely intact.

### 3. 🌐 Web Dashboard with Roblox Avatars
Open `http://localhost:8787` (or your server's IP address) in any browser to access the control panel:
- Real-time project overview (revisions, object counts, member count).
- Connected collaborators with their Roblox avatar headshots and live activities (editing script, selecting parts, viewing place).
- Server uptime, database mode, and health check.
- Safe project deletion with modal confirmation.
- Privacy toggleable directly from Roblox Studio settings.

### 4. ⏱️ Real-Time AFK / Idle Detection
Track who is actively building and who is away from keyboard:
- Detects user input, camera movement, script editing, selection changes, and Undo/Redo.
- When inactive for > 60 seconds, status dynamically updates to `IDLE (Xm Ys)` in the top-right badge, in participant cards, and in overhead 3D player billboards.

### 5. 🔐 Room Passwords & Projects Browser
- Create multiple independent rooms/projects on a single server.
- Protect confidential builds with room passwords.
- Browse and join available server projects directly inside the Studio plugin dock without needing invite codes.

---

## 🏁 Getting Started

### 1. Download & Installation

1. Download **`PlaceTogether.rbxmx`** and **`StartPlaceTogetherServer.bat`** from this repository.
2. In **Roblox Studio**, open the **Plugins** tab and click **Plugin Folder**.
3. Place `PlaceTogether.rbxmx` into this folder.
4. Restart Roblox Studio.

> **⚠️ IMPORTANT:** When prompted by Roblox Studio, grant **HTTP Requests** and **Script Injection** permissions. They are strictly required for network synchronization.

---

### 2. Starting the Server

#### Option A: Windows (Local PC or LAN)
Simply double-click **`StartPlaceTogetherServer.bat`**.
The server will start instantly and display the IP address to enter into the plugin (e.g., `http://192.168.1.65:8787` or `http://127.0.0.1:8787`).

#### Option B: Linux VPS / Cloud Server
```bash
python3 server.py --port 8787
```
Or run as a background service via systemd using `dedicated/placetogether.service`.

#### Option C: Docker
```bash
docker compose -f dedicated/docker-compose.yml up -d
```

---

### 3. Connecting to the Server

1. Open **Roblox Studio**.
2. **Open an empty Baseplate** (or your existing local place file if pushing a new project).
3. Open the **PlaceTogether** plugin dock.
4. Click **Settings** and enter the Server Address (e.g., `http://192.168.1.65:8787`).
5. Go to the **Projects** tab:
   - **Create New Project**: Enter a name, optional password, and upload your place.
   - **Join Existing Project**: Select from the Server Rooms Browser, enter password (if required), and click **Connect**.

---

### 4. Networking & Remote Play

For collaborating over the internet with friends:
- **Virtual LAN (Recommended)**: Use [Radmin VPN](https://www.radmin-vpn.com/), [Hamachi](https://vpn.net/), or [Porthole](https://store.steampowered.com/app/4963920/Porthole_Local_Port_Sharing/). All participants join the same network, and enter the host's virtual IP into the plugin.
- **Port Forwarding / VPS**: Forward TCP port `8787` on your router or host on a cloud VPS.

> 📖 **Подробное руководство на русском языке**:  
> Полная пошаговая инструкция по настройке сервера, VPS, Docker, Radmin VPN и брандмауэра доступна в файле [**`dedicated/DEDICATED_SERVER_GUIDE_RU.md`**](dedicated/DEDICATED_SERVER_GUIDE_RU.md).

---

## ⚙️ Server Command-Line Flags

```text
usage: server.py [-h] [--port PORT] [--host HOST] [--db DB_FILE] [--log LOG_FILE] [--dashboard {on,off,auto}]

Options:
  --port PORT            Server port (default: 8787)
  --host HOST            Bind address (default: 0.0.0.0)
  --db DB_FILE           Path to SQLite database (default: placetogether.db)
  --log LOG_FILE         Path to HTTP access log (default: server.log)
  --dashboard {on,off}   Enable or disable Web Dashboard (default: on)
```

---

## 🤝 Credits & Acknowledgements

- **Original Place Together Creators**: [@simonplay43](https://github.com/Gre4ka43) & [@MNKPO57](https://github.com/MNKPO57).
- **Dedicated Edition & SQLite Backend**: Developed to provide stable, non-destructive, 24/7 collaborative building for teams and animators.
