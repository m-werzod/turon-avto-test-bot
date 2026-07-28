# Turon Avto Test | UZ

Telegram bot that publishes Uzbek driving-licence exam questions to channels as
**quiz polls**, on a schedule, in Tashkent time — and never repeats a question
until the entire bank has been used.

Built to run unattended: it survives restarts, reconnects on its own, keeps
posting when a content source is unreachable, and reports problems to its admins
instead of dying quietly.

---

## Contents

- [What it does](#what-it-does)
- [Question sources: read this first](#question-sources-read-this-first)
- [Quick start (Docker)](#quick-start-docker)
- [Environment variables](#environment-variables)
- [Running locally](#running-locally)
- [Database migrations](#database-migrations)
- [Loading questions](#loading-questions)
- [Admin panel](#admin-panel)
- [How the no-repeat rule works](#how-the-no-repeat-rule-works)
- [Deployment](#deployment)
- [Backups](#backups)
- [Development](#development)
- [Troubleshooting](#troubleshooting)
- [Project layout](#project-layout)

---

## What it does

| | |
|---|---|
| **Publishes** | Telegram quiz polls — question, image, 4 options, correct answer marked, optional explanation |
| **Schedules** | 1–3 posts per day at times you choose, always in `Asia/Tashkent` regardless of server timezone |
| **Never repeats** | Enforced by a database constraint, not by application logic |
| **Cycles** | When every question has been posted, a new cycle starts automatically |
| **Multi-channel** | One post fans out to every connected channel |
| **Bilingual** | Uzbek and Russian interface, chosen per user |
| **Admin-only** | Management panel gated on `ADMIN_IDS`; everyone else is refused |
| **Survives restarts** | Schedule, cycle progress and pause state all live in PostgreSQL |

---

## Question sources: read this first

The original specification named `avtotestu.uz` as the source and expected a
scraper to pull ~1225 questions from it. **That is not something this project
does, and the reason matters.**

Checking the site directly:

- It is a React single-page app backed by Supabase. Questions arrive through an
  authenticated API — there are no per-question pages to crawl.
- **Only variant 1 of 63 is free.** The site states plainly:
  *"Bepul: faqat 1-variant. Qolganlari PRO bilan"* and
  *"Mavzular faqat PRO obunachilar uchun."*
- So essentially the whole bank sits behind a **paid PRO subscription**.

`robots.txt` permits crawling public pages, but that does not extend to content
behind a paywall. Extracting all 1225 questions would mean either bypassing their
access controls or using a paid account to bulk-copy their product and republish
it free to a public channel — which would undercut the business whose content it
is. So the scraper was replaced with something better for you anyway:

**The bot is agnostic about where questions come from.** It imports from JSON,
CSV or XLSX, and adding another provider means implementing one class. Nothing in
the scheduler, poll builder or database changes.

If you want to use `avtotestu.uz` content, contact them about a licence or API
agreement — with access granted, writing a source for it is a small job. See
[Adding a new source](#adding-a-new-source).

A 20-question sample bank ships in `data/sample_questions.json` so you can verify
the whole pipeline immediately. **Delete it once you load real content**, or its
questions will keep appearing in the rotation.

---

## Quick start (Docker)

Requires Docker and Docker Compose. Nothing else — no Python, no PostgreSQL.

```bash
git clone https://github.com/m-werzod/turon-avto-test-bot.git
```

```bash
cd turon-avto-test-bot && cp .env.example .env
```

Edit `.env` and set two values:

```bash
BOT_TOKEN=<from @BotFather>
ADMIN_IDS=<your Telegram id, from @userinfobot>
```

Then:

```bash
docker compose up -d --build
```

That is the entire deployment. PostgreSQL starts, the bot waits for it, applies
migrations, and begins polling.

Watch it come up:

```bash
docker compose logs -f bot
```

Then message your bot `/start` in Telegram, pick a language, and the admin panel
opens.

### First-run checklist

1. **Connect a channel** — add the bot to your channel as an **administrator**
   with *Post messages* enabled, then send the channel username to the bot.
2. **Update tests** → import `sample_questions.json` (or your own file).
3. **Scheduler** → choose 1–3 posts per day and their times.
4. **Send test now** → confirm a quiz poll actually lands in the channel.

---

## Environment variables

Every value the bot reads. Only the first two have no working default.

| Variable | Default | Description |
|---|---|---|
| `BOT_TOKEN` | — | **Required.** Token from [@BotFather](https://t.me/BotFather). |
| `ADMIN_IDS` | — | **Required.** Comma-separated Telegram user ids allowed into the panel. |
| `DATABASE_URL` | — | Async DSN. Compose sets this for you. Must use `postgresql+asyncpg://`. |
| `POSTGRES_USER` | `turon` | Consumed by the `postgres` container. |
| `POSTGRES_PASSWORD` | `turon` | **Change this in production.** |
| `POSTGRES_DB` | `turon_avto` | Database name. |
| `TIMEZONE` | `Asia/Tashkent` | Zone all schedule times are interpreted in. |
| `SCHEDULER_MISFIRE_GRACE` | `3600` | Seconds a missed run may still fire late. |
| `DEFAULT_LANGUAGE` | `uz` | Interface language before a user chooses. |
| `MEDIA_ROOT` | `media/images` | Where question images are cached. |
| `SEND_IMAGES_AS_DOCUMENT` | `false` | `true` sends images uncompressed (original quality, but not inline). |
| `MAX_RETRIES` | `3` | Attempts for network operations. |
| `RETRY_BACKOFF_SECONDS` | `2.0` | Base backoff between attempts. |
| `NOTIFY_ADMINS_ON_ERROR` | `true` | Push error alerts to admins over Telegram. |
| `LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL`. |
| `LOG_DIR` | `logs` | Log directory. |
| `LOG_MAX_BYTES` | `10485760` | Rotate at this size. |
| `LOG_BACKUP_COUNT` | `10` | Rotated files kept. |
| `LOG_FORMAT` | `text` | `text` for humans, `json` for log shippers. |
| `BACKUP_DIR` | `backups` | Where backup archives are written. |

Settings are validated at start-up. A malformed `BOT_TOKEN`, a sync database
driver or an unknown timezone stops the process immediately with a readable
message rather than failing at the first scheduled post.

> **On image quality.** `SEND_IMAGES_AS_DOCUMENT=true` sends the original file
> with no re-encoding, which is what "do not lose image quality" requires — but
> Telegram then shows it as an attachment rather than rendering it inline. For
> road-sign images the default (`false`) usually reads better in a channel. Try
> both and pick.

---

## Quick start (no Docker)

If Docker is not installed, the bot runs on SQLite with nothing else to set up —
the database is a single file. Adequate for one bot posting a few times a day.

```bash
python -m venv .venv && .venv\Scripts\activate
```

```bash
pip install -e ".[sqlite]"
```

Set `DATABASE_URL=sqlite+aiosqlite:///./turon.db` in `.env`, then:

```bash
alembic upgrade head
```

```bash
python scripts/doctor.py
```

`doctor` checks configuration, Telegram auth, the database, and the four things
that make a *running* bot post nothing — empty bank, no channel, no schedule,
scheduler paused — printing the fix for each. Run it whenever something is off.

```bash
python -m bot
```

Switch to PostgreSQL later by changing one line in `.env`; nothing else differs.

---

## Bot branding

Set the profile photo, name, descriptions and command menu in one go. Save your
logo to `assets/logo.png`, then:

```bash
python scripts/setup_bot_profile.py
```

The script squares and re-encodes the image for you, so any square-ish PNG or
JPEG works. It is idempotent — re-run it after changing the logo.

> Most guides say a bot's avatar can only be set by hand via @BotFather
> `/setuserpic`. That is out of date: `setMyProfilePhoto` exists in the Bot API
> and is what this script uses. If your Telegram library predates it, the
> @BotFather route still works as a fallback.

---

## Running locally

Python 3.12+ required.

```bash
python -m venv .venv && . .venv/bin/activate
```

On Windows: `.venv\Scripts\activate`

```bash
pip install -e ".[dev]"
```

Point `DATABASE_URL` at a PostgreSQL instance you can reach, then:

```bash
alembic upgrade head
```

```bash
python -m bot
```

> **Windows note.** `tzdata` is in the dependency list because Windows ships no
> system timezone database — without it, `TIMEZONE=Asia/Tashkent` fails
> validation at start-up.

---

## Database migrations

```bash
alembic upgrade head
```

```bash
alembic downgrade -1
```

```bash
alembic revision --autogenerate -m "describe the change"
```

`alembic/env.py` reads `DATABASE_URL` from the environment, so `alembic.ini`
never contains credentials. Under Docker, migrations are applied automatically on
every start — an upgrade that adds one needs no manual step.

Autogenerate only sees models that have been imported. `env.py` imports
`bot.database.models` for exactly that reason; a model missing from that package
would be silently absent from migrations.

---

## Loading questions

Press **🔄 Testlarni yangilash** in the admin panel, then either pick a file
already in `data/` or upload one directly in the chat.

Import is **idempotent** — running it twice over the same file changes nothing,
and running it over an updated file inserts only what is genuinely new. The key
is `(source, external_id)`, so keep your ids stable.

### JSON

```json
{
  "source": "my_bank",
  "language": "uz",
  "questions": [
    {
      "id": "1",
      "question": "Svetoforning qizil chirog'i nimani bildiradi?",
      "options": ["To'xtash", "Harakat", "Ogohlantirish", "Burilish"],
      "correct": 1,
      "explanation": "Qizil chiroq harakatni taqiqlaydi.",
      "image": "https://example.com/signs/1.png",
      "category": "Svetofor"
    }
  ]
}
```

A bare array works too. `data`, `items`, `records` and `tests` are accepted as
the wrapper key alongside `questions`.

### CSV / XLSX

First row is the header. Delimiter is sniffed, so comma-, semicolon- and
tab-separated files all work — including what Excel produces in a ru/uz locale.

```csv
id,question,option1,option2,option3,option4,correct,explanation,image,category
1,Qizil chiroq nimani bildiradi?,To'xtash,Harakat,Ogohlantirish,Burilish,1,Harakat taqiqlanadi,,Svetofor
```

### Field names

Case-insensitive, and several spellings are accepted per field:

| Field | Accepted names |
|---|---|
| id | `id`, `external_id`, `question_id`, `test_id`, `number`, `no` |
| question | `question`, `text`, `question_text`, `savol`, `vopros`, `title` |
| options | `options`, `answers`, `variants`, `choices`, `javoblar`, or `option1..4`, or `a`/`b`/`c`/`d` |
| explanation | `explanation`, `comment`, `izoh`, `description`, `note` |
| category | `category`, `topic`, `mavzu`, `section`, `tema` |
| image | `image`, `image_url`, `photo`, `picture`, `rasm`, `img` |
| language | `language`, `lang`, `til` |

### Marking the correct answer — read carefully

Three spellings, and they mean **different things**. Guessing between them would
silently mark the wrong option correct, so each is explicit:

| Field | Meaning | Example |
|---|---|---|
| `correct_index` | **0-based** index | `2` → third option |
| `correct` / `answer` | **1-based** number, or a letter | `3` or `"C"` → third option |
| `correct_answer` | the answer as **text**, matched against the options | `"To'xtash"` |

`correct: 0` is **rejected**, not guessed — it is ambiguous between the two
conventions.

### Validation

Each record must have a question, exactly 4 non-empty distinct options, and a
resolvable correct answer. Text is clamped to Telegram's limits automatically
(300 / 100 / 200 characters). Invalid records are **skipped, not fatal** — one
bad row out of 1200 does not cost you the other 1199, and the first few errors
are reported back in the chat with their row numbers.

### Images

Downloaded once at import and cached under `MEDIA_ROOT`, so an unreachable image
host later cannot stop a scheduled post. Type is detected from the file's magic
bytes rather than the server's `Content-Type` header, and anything over 10 MB is
rejected (Telegram's limit). A failed image never fails the import — that
question simply posts as a text-only poll.

### Adding a new source

Implement `QuestionSource` and register it:

```python
from bot.sources.base import QuestionSource, RawQuestion

class MyApiSource(QuestionSource):
    name = "myapi"  # permanent — it is half the natural key

    async def fetch(self):
        for row in await self._call_api():
            yield RawQuestion(
                external_id=str(row["id"]),
                text=row["question"],
                options=row["options"],
                correct_index=row["answer"],
            )

    async def count_estimate(self) -> int | None:
        return None
```

Nothing else changes — the scheduler, poll builder and database are unaffected.

---

## Admin panel

Only ids in `ADMIN_IDS` can open it. Access is checked against the environment on
every update, never against a database column, so a database write can't grant
someone the panel.

| Button | What it does |
|---|---|
| 📡 **Kanalni ulash** | Connect a channel, with permissions verified up front |
| ⏰ **Jadval** | Set 1–3 posting times; toggle weekend posting |
| 📊 **Statistika** | Bank size, cycle progress, sends today, channels, next run, last quiz |
| 🔄 **Testlarni yangilash** | Import questions from `data/` or an upload |
| 🚀 **Hozir yuborish** | Publish the next question immediately, off-schedule |
| ⏸ **Pauza** / ▶️ **Davom** | Suspend or resume automatic posting (survives restart) |
| 💾 **Zaxira nusxa** | Export a full backup and receive it in the chat |
| 📜 **Loglar** | Recent events, error-only filter, download the log tail |
| ⚙️ **Sozlamalar** | Interface language, content language, weekend policy |

Commands: `/start`, `/admin`, `/language`, `/cancel`, `/help`.

### Connecting a channel

The bot verifies before storing anything, so a channel that cannot be posted to
never silently swallows a scheduled slot. It checks, in the order you would fix
them: is it a channel, is the bot a member, is the bot an administrator, does it
hold the right to post.

> In a Telegram **channel**, one administrator right — *Post messages* — governs
> text, media and polls alike. There are no separate per-type toggles the way
> there are for group members, so if that right is missing the bot reports all
> three affected capabilities at once.

If the bot later loses access, that channel is deactivated automatically and the
admins are notified — otherwise every future send would log the same error
forever.

---

## How the no-repeat rule works

The requirement is that a question never repeats until all of them have been
posted, then a new cycle begins.

A **cycle** is one full pass over the bank. Claiming a question inserts a row
into `quiz_posts`, which carries:

```sql
UNIQUE (cycle_id, question_id)
```

That constraint — not application logic — is the guarantee. Application-level
checking would not survive a manual *Send now* racing the 13:00 job, or a crash
between picking a question and recording it. Here the second claim simply fails
at the database and the bot picks again.

Selection is `ORDER BY random()` over questions not yet used in the current
cycle, done in the database so memory stays flat as the bank grows. When nothing
is left, the cycle is closed and the next one opens inline — posting never stops.

Two consequences worth knowing:

- **Questions imported mid-cycle become available immediately.** New content
  reaches the channel promptly instead of waiting weeks for the next cycle.
- **If a send fails on every channel, the question is released back into the
  cycle.** An outage costs a slot, not a question. Keeping the claim would let
  each Telegram blip silently shrink the bank. A *partial* success keeps the
  claim, since the question did reach an audience.

---

## Deployment

**Keeping it running permanently:** see [deploy/README.md](deploy/README.md).
It compares the four options honestly — a Windows Scheduled Task (free, one
command, but stops when the PC sleeps), a VPS with Docker or systemd, and the
managed platforms — and ships the unit file and installer for each.

The short version: the scheduler only fires while the process is alive, and a
desktop is not a server. If a 07:00 post matters, run it somewhere that is awake
at 07:00.


Any host that runs Docker works — Ubuntu VPS, Hetzner, DigitalOcean, Hostinger,
Railway, Render.

### Ubuntu VPS

```bash
curl -fsSL https://get.docker.com | sh
```

```bash
git clone https://github.com/m-werzod/turon-avto-test-bot.git && cd turon-avto-test-bot
```

```bash
cp .env.example .env && nano .env
```

```bash
docker compose up -d --build
```

`restart: unless-stopped` brings the bot back after a reboot, and the scheduler
rebuilds itself from the database on start.

### systemd (without Docker)

```ini
[Unit]
Description=Turon Avto Test Bot
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=turon
WorkingDirectory=/opt/turon-avto-test-bot
EnvironmentFile=/opt/turon-avto-test-bot/.env
ExecStartPre=/opt/turon-avto-test-bot/.venv/bin/alembic upgrade head
ExecStart=/opt/turon-avto-test-bot/.venv/bin/python -m bot
Restart=always
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now turon-bot
```

### Railway / Render

Both build the `Dockerfile` directly. Provision a PostgreSQL add-on, then set
`BOT_TOKEN`, `ADMIN_IDS` and `DATABASE_URL` in the dashboard.

Their `DATABASE_URL` is usually `postgres://…` — rewrite it to
`postgresql+asyncpg://…` or start-up validation will reject it. That check is
deliberate: a sync driver would block the event loop rather than fail loudly.

Run the bot as a **worker**, not a web service. It polls Telegram and never binds
a port.

### Restart behaviour

| On restart | What happens |
|---|---|
| Schedule | Rebuilt from `schedule_slots` |
| Cycle progress | Preserved — a posted question is never re-served |
| Pause state | Preserved — a paused bot stays paused |
| Missed slot | Fired once if it is inside the grace window and nothing was sent since |
| Queued updates | Dropped, so stale button presses do not replay |

The catch-up is deliberately capped at **one** post, so a server that was off for
a week does not dump a backlog into your channel.

---

## Backups

**💾 Zaxira nusxa** produces a timestamped ZIP containing JSON dumps of every
table plus a manifest, and sends it to you in Telegram.

JSON rather than `pg_dump`: the archive stays readable, restores into any
PostgreSQL version, and can be inspected without a database — and the bot
container has no `pg_dump` binary in it.

Images are not bundled. They are re-downloadable from the `image_url` stored on
each question, and including them would push the archive past Telegram's 50 MB
upload limit. Back up the `media_data` volume separately if you want them:

```bash
docker run --rm -v turon-avto-test_media_data:/data -v "$PWD:/out" alpine tar czf /out/media-backup.tar.gz -C /data .
```

A nightly job prunes to the 10 most recent archives and trims event logs older
than 90 days.

---

## Development

```bash
pip install -e ".[dev]"
```

```bash
pytest
```

```bash
ruff check bot/ tests/ && black --check bot/ tests/ -l 100
```

```bash
mypy bot/
```

132 tests. They run against a real SQLite database rather than mocks, so the
constraints and queries carrying the important behaviour are genuinely exercised.
`tests/test_cycle.py` is the one to read first — it exhausts a full bank and
asserts the no-repeat guarantee end to end, including across a simulated restart.

---

## Troubleshooting

**Bot does not start; "Configuration error"**
The message names the offending variable. Usually `BOT_TOKEN` still holds the
placeholder from `.env.example`, or `ADMIN_IDS` is empty.

**"DATABASE_URL must use an async driver"**
Rewrite `postgres://` or `postgresql://` as `postgresql+asyncpg://`. Managed
hosts hand out the sync form.

**"Unknown TIMEZONE 'Asia/Tashkent'"**
Missing system tz database. `pip install tzdata` (already a dependency, so this
means an incomplete install).

**Channel connection says the bot is not an administrator**
Add the bot to the channel *as an admin*, not as a subscriber, and enable
*Post messages*. Being a member is not enough.

**Nothing posts at the scheduled time**
Check in order: is the scheduler paused (the menu shows ▶️ *Davom ettirish* when
it is); is at least one channel connected and active; does the bank still have
questions; is weekend skipping on. **📊 Statistika** shows all four at once.

**Times fire an hour early or late**
Schedule times are always `Asia/Tashkent`, independent of server time. If they
are consistently off, `TIMEZONE` has been overridden.

**Images do not appear**
Only questions whose import supplied an `image` URL have one. **📊 Statistika**
shows how many are illustrated. A failed download leaves the question as a
text-only poll — check `logs/error.log`.

**Same question appeared twice**
Within one cycle this is prevented by a database constraint. Across a cycle
boundary it is expected: the bank was exhausted and a new cycle began. Check the
cycle number in **📊 Statistika**.

**Where are the logs**

```bash
docker compose logs -f bot
```

```bash
docker compose exec bot tail -f logs/error.log
```

Or use **📜 Loglar** in the panel, which works from a phone.

---

## Project layout

```
bot/
├── app.py              Composition root and lifecycle
├── __main__.py         Entrypoint (python -m bot)
├── config/             Validated settings from the environment
├── database/
│   ├── models/         ORM models
│   ├── repositories/   Every query the app makes
│   └── session.py      Async engine
├── handlers/
│   ├── admin/          Admin panel
│   ├── start.py        Public surface
│   └── errors.py       Global error handler
├── keyboards/          Inline keyboards, callback vocabulary
├── middlewares/        Session, user context, admin gate, throttling, logging
├── scheduler/          APScheduler jobs, rebuilt from the database
├── services/           Business logic
├── sources/            Pluggable question sources
├── locales/            uz / ru catalogs
└── utils/              Logging, retry, text limits
alembic/                Migrations
data/                   Question files (bind-mounted in Docker)
docker/                 Entrypoint script
tests/                  132 tests
```

---

## Licence

MIT.

Question content is **not** covered by this licence. You are responsible for
holding the rights to whatever bank you import.
