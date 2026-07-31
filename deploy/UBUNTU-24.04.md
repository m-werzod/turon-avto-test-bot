# Ubuntu 24.04 VPS — production deployment

End-to-end setup for running the bot 24/7: PostgreSQL, Supervisor, cron
housekeeping, automatic restart, and scheduled posting.

Everything below is copy-pasteable in order. Roughly 20 minutes.

**Assumed:** a fresh Ubuntu 24.04 server with root or sudo access. Ubuntu 24.04
ships Python 3.12, which is what this project needs — no PPA, no pyenv.

---

## Contents

1. [Server preparation](#1-server-preparation)
2. [PostgreSQL](#2-postgresql)
3. [Application user and code](#3-application-user-and-code)
4. [Configuration](#4-configuration)
5. [Database migrations](#5-database-migrations)
6. [Telegram Bot API setup](#6-telegram-bot-api-setup)
7. [Supervisor](#7-supervisor)
8. [Cron housekeeping](#8-cron-housekeeping)
9. [Scheduled quiz sending](#9-scheduled-quiz-sending)
10. [Verify it works](#10-verify-it-works)
11. [Updating](#11-updating)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. Server preparation

```bash
sudo apt update && sudo apt upgrade -y
```

```bash
sudo apt install -y python3.12 python3.12-venv python3-pip git postgresql postgresql-contrib supervisor cron
```

**Set the clock to Tashkent.** The bot converts its own schedule into
`Asia/Tashkent` regardless, so this is not required for correct posting — but it
makes every log line and `supervisorctl status` timestamp match the times you set
in the panel, which matters a great deal when something goes wrong at 07:00.

```bash
sudo timedatectl set-timezone Asia/Tashkent && timedatectl
```

Confirm the clock is actually synchronised — a drifting clock posts at the wrong
minute:

```bash
timedatectl show --property=NTPSynchronized --value
```

Should print `yes`. If not: `sudo systemctl enable --now systemd-timesyncd`.

### Firewall

The bot makes only outbound connections. It needs no inbound port beyond SSH.

```bash
sudo ufw allow OpenSSH && sudo ufw --force enable
```

---

## 2. PostgreSQL

SQLite works, but PostgreSQL is what you want here: several users each running
their own schedule means concurrent writes, and SQLite serialises them behind a
single writer lock.

```bash
sudo -u postgres psql -c "CREATE USER turon WITH PASSWORD 'CHANGE_THIS_PASSWORD';"
```

```bash
sudo -u postgres psql -c "CREATE DATABASE turon_avto OWNER turon;"
```

```bash
sudo -u postgres psql -c "ALTER DATABASE turon_avto SET timezone TO 'Asia/Tashkent';"
```

> **Pick a real password.** It goes in `.env` in a moment. Generate one with
> `openssl rand -base64 24` rather than inventing it.

Confirm you can connect as that user:

```bash
PGPASSWORD='CHANGE_THIS_PASSWORD' psql -h 127.0.0.1 -U turon -d turon_avto -c '\conninfo'
```

PostgreSQL listens only on localhost by default on Ubuntu, which is what you
want. Leave `listen_addresses` alone.

---

## 3. Application user and code

A dedicated unprivileged account. The bot never needs root, and a compromised
scraper should not own the machine.

```bash
sudo adduser --system --group --home /opt/turon-avto-test-bot turon
```

```bash
sudo -u turon git clone https://github.com/m-werzod/turon-avto-test-bot.git /opt/turon-avto-test-bot
```

```bash
cd /opt/turon-avto-test-bot && sudo -u turon python3.12 -m venv .venv
```

```bash
sudo -u turon .venv/bin/pip install --upgrade pip && sudo -u turon .venv/bin/pip install -e .
```

Log directory for Supervisor's own capture (the application's rotating logs live
in `logs/` inside the project):

```bash
sudo mkdir -p /var/log/turon-bot && sudo chown turon:turon /var/log/turon-bot
```

---

## 4. Configuration

```bash
sudo -u turon cp .env.example .env && sudo -u turon nano .env
```

Set these four:

```ini
BOT_TOKEN=<from @BotFather>
ADMIN_IDS=<your numeric Telegram id, from @userinfobot>
DATABASE_URL=postgresql+asyncpg://turon:CHANGE_THIS_PASSWORD@127.0.0.1:5432/turon_avto
TIMEZONE=Asia/Tashkent
```

Note `postgresql+asyncpg://`, not `postgres://`. The project uses an async
driver and rejects a synchronous DSN at start-up rather than deadlocking later.

**Lock the file down.** It holds a token that grants full control of your bot
and a database password:

```bash
sudo chmod 600 /opt/turon-avto-test-bot/.env && sudo chown turon:turon /opt/turon-avto-test-bot/.env
```

```bash
ls -l /opt/turon-avto-test-bot/.env
```

Must read `-rw------- 1 turon turon`. Anything with group or world bits set means
every account on the box can read your token.

---

## 5. Database migrations

```bash
sudo -u turon .venv/bin/alembic upgrade head
```

Migrations deliberately need no `BOT_TOKEN` — only `DATABASE_URL` — so schema
work is possible before Telegram is configured, and in CI.

Load the question bank (~1250 questions with images, a few minutes):

```bash
sudo -u turon .venv/bin/python scripts/refresh_questions.py
```

---

## 6. Telegram Bot API setup

### Create the bot

Message [@BotFather](https://t.me/BotFather):

```
/newbot
```

Give it a name and a username ending in `bot`. He replies with the token — that
is what goes in `.env`.

### Privacy mode

Leave **group privacy ON** (the default). The bot only ever reads private
messages to itself and posts to channels; it has no reason to see group traffic,
and Telegram will not deliver what it does not need.

### Branding — logo, description, command menu

```bash
sudo -u turon .venv/bin/python scripts/setup_bot_profile.py --photo assets/logo.png
```

Sets the profile photo, name, Uzbek and Russian descriptions, and the slash-command
menu, all through the Bot API. Drop `--photo` if you have no logo yet.

### Connect a channel

Do this in Telegram, not on the server:

1. Add the bot to your channel as an **administrator**
2. Grant **Post messages** — that alone is enough
3. In the bot, press **Kanalni ulash** and send the channel `@username`

The bot verifies it can actually post before storing anything, so a
misconfigured channel is refused now rather than silently swallowing a 07:00 slot
later.

---

## 7. Supervisor

```bash
sudo cp /opt/turon-avto-test-bot/deploy/supervisor/turon-bot.conf /etc/supervisor/conf.d/
```

```bash
sudo supervisorctl reread && sudo supervisorctl update
```

```bash
sudo supervisorctl status turon-bot
```

Expect `RUNNING`. Watch it start:

```bash
sudo supervisorctl tail -f turon-bot stderr
```

### What the config buys you

| Setting | Why |
|---|---|
| `autorestart=true` | Comes back from a crash |
| `startsecs=15` | Not called "started" until it has survived start-up — a bad token dies in seconds and is reported as failed, not RUNNING |
| `startretries=5` | Then stays FATAL instead of hammering Telegram with a wrong token forever |
| `stopsignal=INT` | The bot's own shutdown path — scheduler stopped, pool drained |
| `stopwaitsecs=30` | Enough time to finish, before SIGKILL |

Supervisor also starts the bot on boot, because `supervisor` itself is a systemd
service that is enabled by default. Confirm:

```bash
systemctl is-enabled supervisor
```

---

## 8. Cron housekeeping

```bash
sudo cp /opt/turon-avto-test-bot/deploy/cron/turon-bot-cron /etc/cron.d/turon-bot
```

```bash
sudo chmod 644 /etc/cron.d/turon-bot && sudo chmod +x /opt/turon-avto-test-bot/deploy/cron/watchdog.sh
```

```bash
sudo systemctl restart cron
```

Four jobs:

| When | What |
|---|---|
| 03:15 daily | `pg_dump` of the database into `backups/` |
| 03:45 daily | Delete dumps older than 14 days |
| 04:30 Sunday | Refresh the question bank from the sources |
| every 10 min | Watchdog — restart if the heartbeat goes stale |

**Quiz posting is not among them, deliberately.** See the next section.

### The watchdog, and why it exists

Supervisor restarts a process that *exits*. It cannot tell that a process still
holding its PID has stopped doing anything — a wedged event loop and a quietly
idle one look identical from outside.

So the bot writes `logs/heartbeat` every minute. The watchdog restarts it if that
timestamp stops advancing for five minutes, and does nothing while the program is
deliberately stopped, so it will not fight you during maintenance.

---

## 9. Scheduled quiz sending

**Posting is not a cron job.** It runs inside the bot, on APScheduler.

That is a deliberate design choice, not an oversight. Every user picks their own
times from the panel and changes them whenever they like. A cron entry would have
to be rewritten on every edit, would have no way to know *whose* schedule it was
firing, and would run a second Python process against a bot that must only ever
have one.

What you get instead:

- **Per user.** Each person's times, batch size and pause state are their own.
- **One trigger per distinct time.** A thousand users posting at 08:00 share a
  single cron trigger inside the scheduler, not a thousand.
- **Tashkent time**, whatever the server clock says.
- **Catch-up.** A slot missed while the process was down still fires on start-up,
  if it is inside the misfire window. At most once — a server that was off for a
  week does not dump a backlog into the channel.
- **No overlap.** `max_instances=1`, so a slow batch cannot collide with the next
  slot and double-post.

### Setting a schedule

In the bot: **Jadval → Vaqtlarni o'zgartirish → 1/2/3 → hour → minute**, then
**Bir martada nechta test** for how many questions go out each time.

### Confirming it will fire

```bash
sudo -u turon /opt/turon-avto-test-bot/.venv/bin/python /opt/turon-avto-test-bot/scripts/doctor.py
```

Prints each user's channels, schedule, batch size, pause state and cycle
progress. The four things that make a *running* bot post nothing — empty bank, no
channel, no schedule, scheduler paused — each get a line.

---

## 10. Verify it works

```bash
sudo -u turon /opt/turon-avto-test-bot/.venv/bin/python /opt/turon-avto-test-bot/scripts/doctor.py
```

Then, in Telegram:

1. `/start` — the logo and menu appear
2. **Hozir yuborish** — a quiz poll lands in the channel, image and options in one
   message

Check the heartbeat is beating:

```bash
watch -n 5 'stat -c "%y" /opt/turon-avto-test-bot/logs/heartbeat'
```

Prove the restart works:

```bash
sudo supervisorctl status turon-bot && sudo pkill -f 'python -u -m bot' && sleep 20 && sudo supervisorctl status turon-bot
```

It should be `RUNNING` again with a new PID.

---

## 11. Updating

```bash
cd /opt/turon-avto-test-bot && sudo -u turon git pull
```

```bash
sudo -u turon .venv/bin/pip install -e . && sudo -u turon .venv/bin/alembic upgrade head
```

```bash
sudo supervisorctl restart turon-bot
```

Take a dump first if the release contains migrations:

```bash
sudo -u turon pg_dump -Fc turon_avto > /opt/turon-avto-test-bot/backups/pre-upgrade-$(date +%F).dump
```

---

## 12. Troubleshooting

### `Another instance of the bot is already running`

Working as intended. Telegram permits one poller per token, and a second copy
makes both unreliable without either failing outright — so the second refuses to
start. Find the first:

```bash
sudo supervisorctl status turon-bot && ps aux | grep '[p]ython -u -m bot'
```

Most often a manual `python -m bot` left running in a detached SSH session, or an
old systemd unit still enabled from a previous deployment.

### Bot is RUNNING but does not answer

```bash
sudo supervisorctl tail -100 turon-bot stderr
```

Look for `TelegramConflictError`. That is two pollers — see above.

### Nothing posts at the scheduled time

```bash
sudo -u turon /opt/turon-avto-test-bot/.venv/bin/python /opt/turon-avto-test-bot/scripts/doctor.py
```

In order of likelihood: scheduler paused, no schedule set, no channel connected,
bank empty. The doctor names which.

### `FATAL` in supervisorctl status

It failed to start five times. The reason is in the log:

```bash
sudo tail -50 /var/log/turon-bot/stderr.log
```

Usually a malformed `.env` — the bot validates configuration before anything else
and names the offending value.

### Database connection refused

```bash
sudo systemctl status postgresql && sudo -u turon psql "$(grep DATABASE_URL /opt/turon-avto-test-bot/.env | cut -d= -f2- | sed 's|postgresql+asyncpg|postgresql|')" -c '\conninfo'
```

### Logs

| What | Where |
|---|---|
| Application | `/opt/turon-avto-test-bot/logs/app.log` |
| Errors only | `/opt/turon-avto-test-bot/logs/error.log` |
| Start-up / crashes | `/var/log/turon-bot/stderr.log` |
| Cron and watchdog | `/var/log/turon-bot/cron.log` |

The application's own logs rotate at 10 MB, keeping ten files.

---

## Security checklist

- [ ] `.env` is `600` and owned by `turon`
- [ ] PostgreSQL password is not the one from this document
- [ ] The bot runs as `turon`, never root
- [ ] `ufw` is enabled with only SSH open
- [ ] Backups are being written to `backups/` — check after the first 03:15
- [ ] The token has never been committed to git

On that last point: `.env` is git-ignored, but `.env.example` is not. Never put a
real token in it.
