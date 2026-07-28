# assets

Branding files used by `scripts/setup_bot_profile.py`.

## logo.png

Save your bot logo here as **`logo.png`**, then run:

```bash
python scripts/setup_bot_profile.py
```

The script handles the fiddly parts for you:

- **Centre-crops to a square.** Telegram renders profile photos in a circle and
  crops a non-square upload server-side, so doing it locally makes the result
  predictable rather than a surprise.
- **Flattens transparency onto white**, because the JPEG it uploads cannot carry
  an alpha channel.
- **Resizes to 1024×1024** and re-encodes below Telegram's upload limit.

So any reasonably sized square-ish PNG or JPEG works — you do not need to
pre-process it.

Point it somewhere else with `--photo path/to/file.png` if you prefer.

## Why this folder is mostly empty

The logo is not committed. It is a binary that changes independently of the code,
and keeping it out of git avoids bloating the repository history with successive
versions of the same image. Anyone deploying supplies their own.
