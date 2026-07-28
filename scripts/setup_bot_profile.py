"""One-shot branding: profile photo, name, descriptions and command menu.

Run once after filling in ``BOT_TOKEN``:

    python scripts/setup_bot_profile.py --photo assets/logo.png

Everything here is set through the Bot API, so none of it needs @BotFather.
``setMyProfilePhoto`` is a recent addition — older guides tell you to use
``/setuserpic`` in @BotFather, which still works but cannot be scripted.

The script is idempotent: running it again simply overwrites the same fields, so
it is safe to re-run after tweaking the logo.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import sys
from pathlib import Path

# Allow `python scripts/setup_bot_profile.py` from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BufferedInputFile,
    InputProfilePhotoStatic,
)

from bot.config.settings import load_settings_or_exit
from bot.utils.logging import get_logger, setup_logging

logger = get_logger("setup_bot_profile")

# --- Branding text -----------------------------------------------------------
# Telegram enforces these limits and rejects anything longer.
BOT_NAME = "Turon Avto Test | UZ"  # <= 64 chars

SHORT_DESCRIPTION = {  # <= 120 chars — shown on the bot's profile card
    "uz": "Haydovchilik guvohnomasi imtihoniga tayyorgarlik. Har kuni yangi testlar.",
    "ru": "Подготовка к экзамену на водительские права. Новые тесты каждый день.",
}

DESCRIPTION = {  # <= 512 chars — shown in an empty chat before /start
    "uz": (
        "🚗 Turon Avto Test\n\n"
        "Yo'l harakati qoidalari bo'yicha testlar kanalimizga avtomatik "
        "joylanadi.\n\n"
        "✅ Har bir savol rasm bilan\n"
        "✅ 4 ta variant va to'g'ri javob\n"
        "✅ Har kuni belgilangan vaqtda\n\n"
        "Boshlash uchun /start ni bosing."
    ),
    "ru": (
        "🚗 Turon Avto Test\n\n"
        "Тесты по правилам дорожного движения автоматически публикуются в "
        "нашем канале.\n\n"
        "✅ Каждый вопрос с изображением\n"
        "✅ 4 варианта и правильный ответ\n"
        "✅ Ежедневно в заданное время\n\n"
        "Нажмите /start, чтобы начать."
    ),
}

COMMANDS = {
    "uz": [
        BotCommand(command="start", description="Botni ishga tushirish"),
        BotCommand(command="admin", description="Boshqaruv paneli"),
        BotCommand(command="language", description="Tilni o'zgartirish"),
        BotCommand(command="cancel", description="Amalni bekor qilish"),
        BotCommand(command="help", description="Yordam"),
    ],
    "ru": [
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="admin", description="Панель управления"),
        BotCommand(command="language", description="Сменить язык"),
        BotCommand(command="cancel", description="Отменить действие"),
        BotCommand(command="help", description="Помощь"),
    ],
}

# --- Image handling ----------------------------------------------------------
#: Telegram renders profile photos as a circle from a square source. A
#: non-square upload gets centre-cropped by the server anyway, so cropping here
#: makes the result predictable instead of a surprise.
TARGET_SIZE = 1024
MAX_UPLOAD_BYTES = 5 * 1024 * 1024


def prepare_photo(path: Path) -> bytes:
    """Load an image and return square JPEG bytes ready for upload.

    Args:
        path: Source image, any format Pillow can read.

    Returns:
        Encoded JPEG bytes.

    Raises:
        SystemExit: The file is missing or not a readable image.
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a declared dependency
        print("Pillow is required. Run: pip install -e .", file=sys.stderr)
        raise SystemExit(1) from None

    if not path.exists():
        print(f"Image not found: {path}", file=sys.stderr)
        raise SystemExit(1)

    try:
        image = Image.open(path)
    except Exception as exc:
        print(f"Could not read {path.name} as an image: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    # Flatten transparency onto white; a JPEG cannot carry an alpha channel and
    # the logo has a solid background anyway.
    if image.mode in ("RGBA", "LA", "P"):
        image = image.convert("RGBA")
        canvas = Image.new("RGB", image.size, (255, 255, 255))
        canvas.paste(image, mask=image.split()[-1])
        image = canvas
    else:
        image = image.convert("RGB")

    width, height = image.size
    if width != height:
        side = min(width, height)
        left = (width - side) // 2
        top = (height - side) // 2
        image = image.crop((left, top, left + side, top + side))
        print(f"  cropped {width}x{height} -> {side}x{side} (centre square)")

    if image.width > TARGET_SIZE:
        image = image.resize((TARGET_SIZE, TARGET_SIZE), Image.LANCZOS)
        print(f"  resized to {TARGET_SIZE}x{TARGET_SIZE}")

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=95, optimize=True)
    payload = buffer.getvalue()

    if len(payload) > MAX_UPLOAD_BYTES:
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=85, optimize=True)
        payload = buffer.getvalue()

    print(f"  prepared {len(payload) / 1024:.0f} KB JPEG")
    return payload


# --- Steps -------------------------------------------------------------------


async def apply_photo(bot: Bot, path: Path) -> bool:
    """Upload the profile photo. Returns whether it succeeded."""
    print(f"\nProfile photo — {path.name}")
    payload = prepare_photo(path)

    try:
        await bot.set_my_profile_photo(
            photo=InputProfilePhotoStatic(photo=BufferedInputFile(payload, filename="profile.jpg"))
        )
    except TelegramAPIError as exc:
        print(f"  FAILED: {exc}")
        print("  Fallback: open @BotFather -> /setuserpic and upload it by hand.")
        return False

    print("  set OK")
    return True


async def apply_text(bot: Bot) -> bool:
    """Set name, short description and description in both languages."""
    ok = True

    print("\nName")
    try:
        await bot.set_my_name(name=BOT_NAME)
        print(f"  {BOT_NAME!r} OK")
    except TelegramAPIError as exc:
        # Telegram rate-limits name changes hard; that is not worth aborting for.
        print(f"  skipped: {exc}")
        ok = False

    for label, values, setter in (
        ("Short description", SHORT_DESCRIPTION, "set_my_short_description"),
        ("Description", DESCRIPTION, "set_my_description"),
    ):
        print(f"\n{label}")
        for language, text in values.items():
            try:
                kwargs = {
                    "language_code": language,
                    (
                        "short_description"
                        if setter == "set_my_short_description"
                        else "description"
                    ): text,
                }
                await getattr(bot, setter)(**kwargs)
                print(f"  {language}: OK ({len(text)} chars)")
            except TelegramAPIError as exc:
                print(f"  {language}: FAILED — {exc}")
                ok = False

    return ok


async def apply_commands(bot: Bot) -> bool:
    """Publish the slash-command menu for private chats."""
    print("\nCommand menu")
    ok = True
    for language, commands in COMMANDS.items():
        try:
            await bot.set_my_commands(
                commands=commands,
                scope=BotCommandScopeAllPrivateChats(),
                language_code=language,
            )
            print(f"  {language}: {len(commands)} commands OK")
        except TelegramAPIError as exc:
            print(f"  {language}: FAILED — {exc}")
            ok = False
    return ok


async def run(photo_path: Path | None, skip_photo: bool) -> int:
    """Apply the branding. Returns a process exit code."""
    settings = load_settings_or_exit()
    bot = Bot(token=settings.bot_token)

    try:
        try:
            me = await bot.get_me()
        except TelegramAPIError as exc:
            print(f"\nCould not authenticate with Telegram: {exc}", file=sys.stderr)
            print("Check that BOT_TOKEN in .env is correct.", file=sys.stderr)
            return 1

        print(f"Authenticated as @{me.username} (id {me.id})")

        results = []
        if not skip_photo and photo_path is not None:
            results.append(await apply_photo(bot, photo_path))
        results.append(await apply_text(bot))
        results.append(await apply_commands(bot))

        print("\n" + "-" * 60)
        if all(results):
            print(f"Branding applied. Open https://t.me/{me.username} to see it.")
            print("Telegram caches profile photos — force-close the app if it looks stale.")
            return 0

        print("Finished with some steps skipped or failed; see above.")
        return 1
    finally:
        await bot.session.close()


def main() -> None:
    """Parse arguments and run."""
    parser = argparse.ArgumentParser(
        description="Set the bot's profile photo, name, descriptions and commands."
    )
    parser.add_argument(
        "--photo",
        type=Path,
        default=Path("assets/logo.png"),
        help="Logo image (default: assets/logo.png)",
    )
    parser.add_argument(
        "--skip-photo",
        action="store_true",
        help="Only apply text branding and commands.",
    )
    args = parser.parse_args()

    settings = load_settings_or_exit()
    setup_logging(level="WARNING", log_dir=settings.log_dir)

    raise SystemExit(asyncio.run(run(args.photo, args.skip_photo)))


if __name__ == "__main__":
    main()
