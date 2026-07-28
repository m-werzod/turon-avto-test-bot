# Running the bot permanently

The scheduler only fires while the bot process is alive. Everything else in this
project is built to survive a restart — the schedule, the cycle, and the
catch-up for a slot missed while the process was down — but none of that helps
if nothing is running the process.

So the question is only ever: **what keeps the process alive, and how long is
that thing itself alive?**

---

## The honest comparison

| | Runs when the PC is off | Cost | Setup |
|---|---|---|---|
| **Windows Scheduled Task** | ❌ no | free | 1 command |
| **VPS + systemd** | ✅ yes | ~$4–6/month | ~15 minutes |
| **VPS + Docker** | ✅ yes | ~$4–6/month | ~10 minutes |
| **Railway / Render** | ✅ yes | free tier, then ~$5 | ~5 minutes |

A desktop is not a server. A Scheduled Task survives a crash and a logout, but
not the machine sleeping, restarting for updates, or losing power — and Windows
does all three on its own schedule. If a 07:00 post matters, it has to run
somewhere that is awake at 07:00.

---

## Option 1 — this Windows PC (free, 1 command)

```powershell
powershell -ExecutionPolicy Bypass -File deploy\install-windows-task.ps1
```

Starts the bot at every login and restarts it within a minute if it stops.

```powershell
Start-ScheduledTask -TaskName TuronAvtoTestBot          # start now
Get-ScheduledTask -TaskName TuronAvtoTestBot | Get-ScheduledTaskInfo   # check
powershell -File deploy\install-windows-task.ps1 -Remove               # undo
```

Worth doing even if you plan to move to a VPS — it costs one command and stops
the bot dying every time you close a terminal.

**To reduce the gaps:** turn off sleep (`Settings → System → Power → Screen and
sleep → Never`) and set active hours for Windows Update.

---

## Option 2 — VPS with Docker (recommended)

Any provider works. Hetzner CX22 (~€4/mo), Contabo, DigitalOcean, Hostinger.
Pick one near your users; for Uzbekistan, a European region is fine.

```bash
ssh root@YOUR_SERVER_IP
```

```bash
curl -fsSL https://get.docker.com | sh
```

```bash
git clone https://github.com/m-werzod/turon-avto-test-bot.git /opt/turon-avto-test-bot && cd /opt/turon-avto-test-bot
```

```bash
cp .env.example .env && nano .env
```

Fill in `BOT_TOKEN` and `ADMIN_IDS`, then:

```bash
docker compose up -d --build
```

`restart: unless-stopped` is already set in `docker-compose.yml`, so the bot
comes back after a crash *and* after the server reboots.

```bash
docker compose logs -f bot
```

---

## Option 3 — VPS without Docker (systemd)

```bash
sudo adduser --system --group --home /opt/turon-avto-test-bot turon
sudo -u turon git clone https://github.com/m-werzod/turon-avto-test-bot.git /opt/turon-avto-test-bot
cd /opt/turon-avto-test-bot
sudo -u turon python3 -m venv .venv
sudo -u turon .venv/bin/pip install -e ".[sqlite]"
sudo -u turon cp .env.example .env && sudo -u turon nano .env
sudo -u turon .venv/bin/alembic upgrade head
```

```bash
sudo cp deploy/turon-bot.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now turon-bot
```

```bash
journalctl -u turon-bot -f
```

---

## Option 4 — Railway or Render (no server to manage)

Both deploy straight from the GitHub repository.

1. Create a project and point it at `m-werzod/turon-avto-test-bot`
2. Set `BOT_TOKEN`, `ADMIN_IDS` and `DATABASE_URL` as environment variables
3. Add their managed PostgreSQL and paste its connection string into
   `DATABASE_URL` — but change the scheme to `postgresql+asyncpg://`, because
   they hand out `postgres://` and this project uses an async driver
4. Start command: `alembic upgrade head && python -m bot`

⚠️ **Free tiers sleep.** Both providers idle a free service after inactivity, and
a sleeping bot posts nothing. A bot has no incoming web traffic to keep it awake,
so the free tier will not do — use a paid instance or a VPS.

---

## Moving the questions with you

The bank is 1580 questions and ~880 images, and you do not have to scrape again.

Simplest: press **Testlarni yangilash** on the new deployment and let it
re-import. It takes a couple of minutes, and the upsert is idempotent — the same
questions land with the same ids rather than duplicating.

To carry the exact bank across instead, press **Zaxira nusxa** in the panel. The
bot sends you the archive in Telegram and also leaves it in `backups/`; copy that
to the new machine before first start.

Either way, `media/` holds the downloaded images. Copying it saves re-downloading
~880 files, but it is optional — anything missing is fetched on the next import.

---

## After moving: stop the old one

Two processes on one token both call `getUpdates`, Telegram rejects one of them,
and the bot appears to work intermittently. Whichever way you deploy, make sure
exactly one copy is running.

```powershell
powershell -File deploy\install-windows-task.ps1 -Remove
```
