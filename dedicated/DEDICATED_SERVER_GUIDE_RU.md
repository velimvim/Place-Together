# Руководство по развертыванию выделенного 24/7 сервера PlaceTogether

> 🌐 **[Read this guide in English](DEDICATED_SERVER_GUIDE.md)**

Это руководство объясняет, как запустить сервер **PlaceTogether 3.2 (SQLite Edition)** на удаленном выделенном сервере (Linux VPS / VDS или Windows Server), чтобы система работала **автономно 24/7** без привязки к компьютеру конкретного игрока-хоста.

---

## 🌟 Как теперь устроена автономная система

Раньше для совместной работы требовалось, чтобы «Хост» держал открытым Roblox Studio и отправлял приглашения.

В версии **PlaceTogether 3.2**:
1. **Надежная база данных SQLite 3 (WAL-режим):** вместо медленных и уязвимых текстовых JSON-файлов сервер использует промышленную СУБД SQLite с журналированием транзакций Write-Ahead Logging.
2. **Нулевой риск потери данных (ACID):** каждое перемещение блока фиксируется атомарно за < 1 миллисекунды. Даже при аварийном отключении питания ни один блок не пропадет.
3. **Автоматическая миграция:** если на сервере уже был старый файл `data.json`, сервер автоматически переносит все комнаты и объекты в `placetogether.db` при первом запуске.
4. **Сервер полностью автономен 24/7:** проект хранится постоянно. Любой участник может зайти и продолжить стройку в любое время дня и ночи.
5. **Защита комнат паролями:** доступ к синхронизации защищен криптографическими сессионными токенами (Bearer Token).
6. **Пакетная передача (Чанкинг):** большие карты скачиваются и отправляются пачками по объектам, что исключает ошибки лимитов `HttpService`.
7. **Zero Dependencies:** 100% чистый Python 3 стандартной библиотеки (`sqlite3`, `http.server`).

---

## Вариант 1. Развертывание на Linux через Docker (Рекомендуется)

Самый быстрый способ развернуть сервер на любом Linux VPS (Ubuntu/Debian) — использовать готовую конфигурацию Docker.

### 1. Скопируйте файлы на сервер
Загрузите на VPS папку с проектом (или склонируйте репозиторий):
```bash
mkdir -p /opt/placetogether
cd /opt/placetogether
```
Убедитесь, что в директории лежат файлы:
- `server.py`
- `Dockerfile`
- `docker-compose.yml`

### 2. Запустите контейнер
```bash
docker compose up -d --build
```

Сервер запустится в фоне и будет автоматически перезапускаться при перезагрузке ОС.
База данных проекта будет сохраняться в локальную папку `./data/placetogether.db`.

Посмотреть логи сервера:
```bash
docker compose logs -f
```

Остановить сервер:
```bash
docker compose down
```

---

## Вариант 2. Нативный запуск на Linux через systemd (без Docker)

Если вы не используете Docker, сервер можно запустить как нативный сервис Linux через `systemd`.

### 1. Установите Python 3 (если еще не установлен)
```bash
sudo apt update
sudo apt install -y python3
```

### 2. Разместите файлы проекта
Создайте папку и поместите туда `server.py`:
```bash
sudo mkdir -p /opt/placetogether
sudo cp server.py /opt/placetogether/
sudo chmod +x /opt/placetogether/server.py
```

### 3. Установите systemd-сервис
Скопируйте предоставленный файл `placetogether.service`:
```bash
sudo cp placetogether.service /etc/systemd/system/
```
*(Если пути отличаются, отредактируйте `/etc/systemd/system/placetogether.service`)*.

### 4. Включите автозапуск и запустите сервис
```bash
sudo systemctl daemon-reload
sudo systemctl enable placetogether
sudo systemctl start placetogether
```

Проверить статус:
```bash
sudo systemctl status placetogether
```

Посмотреть логи в реальном времени:
```bash
journalctl -u placetogether -f
```

---

## Вариант 3. Развертывание на Windows Server

1. Установите Python 3.10+ с официального сайта [python.org](https://www.python.org/downloads/) (обязательно отметьте галочку **"Add python.exe to PATH"**).
2. Поместите файлы `server.py` и `StartPlaceTogetherServer.bat` в удобную папку (например, `C:\PlaceTogether`).
3. Запустите `StartPlaceTogetherServer.bat` от имени Администратора (чтобы создалось правило в Windows Firewall для порта 8787).
4. Сервер запустится и будет слушать `0.0.0.0:8787`.

*Для работы в фоне без открытой консоли можно использовать утилиту [NSSM (Non-Sucking Service Manager)](https://nssm.cc/) и зарегистрировать `server.py` как службу Windows.*

---

## Настройка Firewall (Брандмауэра) на сервере

Сервер слушает TCP-порт **8787**. Убедитесь, что порт открыт в брандмауэре вашего хостинга:

### Для Linux (UFW):
```bash
sudo ufw allow 8787/tcp
sudo ufw reload
```

### Для облачных провайдеров (VK Cloud, Selectel, Timeweb, Hetzner, AWS, GCP, DigitalOcean и др.):
Откройте входящий трафик (Inbound Rule) для **TCP-порта 8787** в панели управления хостингом (Security Groups / Сетевой экран).

---

## (Опционально) Настройка красивого домена и HTTPS через Nginx

По умолчанию Roblox Studio умеет отправлять запросы как по `http://`, так и по `https://`. Если вы хотите подключить домен (например, `https://roblox.myteam.com`), настройте Nginx с сертификатом Let's Encrypt:

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

## 🎮 Как теперь работать в Roblox Studio

После запуска выделенного сервера:

### 1. Установите обновленный плагин
Скопируйте `PlaceTogether.rbxmx` в папку плагинов Roblox Studio (`Plugins -> Plugin Folder`) и перезапустите Studio.

### 2. Укажите адрес сервера
1. Откройте вкладку **Settings** в окне плагина.
2. В поле **SERVER ADDRESS** введите адрес вашего сервера:
   * Например: `http://194.87.123.45:8787` (или ваш домен `https://roblox.myteam.com`).
3. Плагин автоматически подключится к серверу и покажет статус `READY`.

### 3. Создание проекта на сервере (первый разработчик)
1. Откройте в Roblox Studio плейс, который хотите перенести на сервер.
2. Перейдите на вкладку **Projects** в плагине.
3. В блоке **Create New Project**:
   * Введите имя проекта (например, `MainGame`).
   * Введите пароль (рекомендуется для защиты от посторонних).
   * Нажмите кнопку **Create & Upload Place**.
4. Плагин создаст комнату на сервере и выгрузит туда текущую карту. Сервер присвоит комнате уникальный ID (например, `rm_3f9a`).

### 4. Подключение к проекту (другие разработчики)
1. Откройте Roblox Studio с **пустым Baseplate** (`File -> New`).
2. В плагине на вкладке **Projects**:
   * В блоке **Server Rooms Browser** нажмите **Refresh Projects List** (вы увидите созданный проект).
   * Нажмите **Select Project** (или введите ID комнаты вручную).
   * Если установлен пароль — введите его в поле **ROOM PASSWORD**.
   * Нажмите **Connect to Project**.
3. Плагин безопасно авторизуется на сервере, скачает карту и включит двустороннюю синхронизацию в реальном времени.

### 5. Автономность сессии
* Любой участник может в любой момент закрыть Roblox Studio — проект останется на сервере.
* Когда разработчик вернется завтра, достаточно открыть пустой Baseplate, выбрать этот же проект и нажать **Connect to Project** — Studio моментально загрузит последнюю версию карты.
