import asyncio
import base64
import binascii
import glob
import logging
import os
import re
import uuid

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, FSInputFile
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from dotenv import load_dotenv
import yt_dlp

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "Не задано BOT_TOKEN. Створи файл .env на основі .env.example")

# Адрес локального Bot API Server (поднимается отдельно, см. README).
# Пусто / не задано — работаем через api.telegram.org.
LOCAL_API_BASE_URL = os.getenv("LOCAL_API_BASE_URL", "").strip()
USE_LOCAL_API = bool(LOCAL_API_BASE_URL)

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# YouTube блокирует запросы с IP дата-центров ("Sign in to confirm you're not a
# bot"). Основной обход — PO token (proof-of-origin): его выдаёт отдельный
# сервис bgutil-ytdlp-pot-provider, аккаунт и cookies при этом не нужны.
# Адрес вида http://pot-provider.railway.internal:4416
POT_PROVIDER_URL = os.getenv("POT_PROVIDER_URL", "").strip()

# Резервный путь — cookies залогиненного аккаунта. Файл даёт полный доступ к
# аккаунту, поэтому используй одноразовый профиль, а не основной.
# COOKIES_FILE — путь к файлу в формате Netscape.
# YOUTUBE_COOKIES_B64 — тот же файл в base64, для хостинга без доступа к ФС.
COOKIES_FILE = os.getenv("COOKIES_FILE", "cookies.txt").strip()
COOKIES_B64 = os.getenv("YOUTUBE_COOKIES_B64", "").strip()

# На локальном Bot API Server лимит на отправку файлов — 2 ГБ вместо 50 МБ
MAX_FILESIZE_MB = 2000 if USE_LOCAL_API else 50

YOUTUBE_URL_RE = re.compile(
    r"(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)[\w\-]+"
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Одновременная конвертация в несколько потоков душит shared-CPU контейнер
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(1)

BOT_CHECK_MARKERS = ("confirm you", "not a bot", "sign in to")


def _materialize_cookies() -> str | None:
    """Готовит cookies-файл для yt-dlp. Возвращает путь либо None."""
    if COOKIES_B64:
        try:
            data = base64.b64decode(COOKIES_B64, validate=True)
        except (binascii.Error, ValueError):
            logger.error(
                "YOUTUBE_COOKIES_B64 не является корректным base64 — cookies проигнорированы"
            )
        else:
            try:
                with open(COOKIES_FILE, "wb") as fh:
                    fh.write(data)
            except OSError:
                logger.exception("Не удалось записать cookies в %s", COOKIES_FILE)
            else:
                logger.info("Cookies записаны из YOUTUBE_COOKIES_B64 в %s", COOKIES_FILE)
                return COOKIES_FILE

    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        logger.info("Использую cookies из %s", COOKIES_FILE)
        return COOKIES_FILE

    logger.info("Cookies не заданы — работаем без них.")
    return None


COOKIES_PATH = _materialize_cookies()

if POT_PROVIDER_URL:
    logger.info("PO token provider: %s", POT_PROVIDER_URL)
elif not COOKIES_PATH:
    logger.warning(
        "Ни POT_PROVIDER_URL, ни cookies не заданы. На IP дата-центра YouTube, "
        "скорее всего, ответит 'Sign in to confirm you're not a bot'."
    )

if USE_LOCAL_API:
    local_server = TelegramAPIServer.from_base(LOCAL_API_BASE_URL, is_local=True)
    bot = Bot(token=BOT_TOKEN, session=AiohttpSession(api=local_server))
    logger.info(
        "Локальный Bot API Server: %s (лимит %d МБ)", LOCAL_API_BASE_URL, MAX_FILESIZE_MB
    )
else:
    bot = Bot(token=BOT_TOKEN)
    logger.info(
        "api.telegram.org (лимит %d МБ). Задай LOCAL_API_BASE_URL для лимита 2 ГБ.",
        MAX_FILESIZE_MB,
    )

dp = Dispatcher()


def extract_youtube_url(text: str) -> str | None:
    match = YOUTUBE_URL_RE.search(text)
    return match.group(0) if match else None


def download_audio(url: str, out_path_no_ext: str) -> str:
    """Скачивает аудиодорожку и конвертирует в mp3. Возвращает путь к файлу."""
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": out_path_no_ext + ".%(ext)s",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    if COOKIES_PATH:
        ydl_opts["cookiefile"] = COOKIES_PATH

    if POT_PROVIDER_URL:
        # Плагин bgutil-ytdlp-pot-provider читает base_url из extractor_args
        # и сам запрашивает PO token у сервиса перед обращением к YouTube
        ydl_opts["extractor_args"] = {
            "youtubepot-bgutilhttp": {"base_url": [POT_PROVIDER_URL]}
        }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        # download=False сначала: 192 kbps ≈ 24 КБ/с, поэтому по длительности
        # видно размер до того, как тратить CPU и трафик на конвертацию
        probe = ydl.extract_info(url, download=False)
        duration = probe.get("duration") or 0
        estimated_mb = duration * 24 / 1024
        if estimated_mb > MAX_FILESIZE_MB:
            raise ValueError(
                f"Відео задовге: ~{estimated_mb:.0f} МБ при ліміті {MAX_FILESIZE_MB} МБ"
            )

        info = ydl.extract_info(url, download=True)
        title = info.get("title", "audio")
    return out_path_no_ext + ".mp3", title


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Привіт! Надішли посилання на відео з YouTube — поверну аудіодоріжку в mp3."
    )


@dp.message(F.text)
async def handle_message(message: Message):
    url = extract_youtube_url(message.text)
    if not url:
        return

    status_msg = await message.answer("Завантажую аудіо, зачекай трохи…")

    file_id = str(uuid.uuid4())
    out_path_no_ext = os.path.join(DOWNLOAD_DIR, file_id)

    try:
        loop = asyncio.get_running_loop()
        async with DOWNLOAD_SEMAPHORE:
            mp3_path, title = await loop.run_in_executor(
                None, download_audio, url, out_path_no_ext
            )

        size_mb = os.path.getsize(mp3_path) / (1024 * 1024)
        if size_mb > MAX_FILESIZE_MB:
            await status_msg.edit_text(
                f"Файл вийшов {size_mb:.1f} МБ — це більше ліміту Telegram "
                f"({MAX_FILESIZE_MB} МБ) для ботів. Спробуй коротше відео."
            )
            return

        audio_file = FSInputFile(mp3_path, filename=f"{title}.mp3")
        await message.answer_audio(audio_file, title=title)
        await status_msg.delete()

    except Exception as e:
        logger.exception("Ошибка при обработке %s", url)
        if any(marker in str(e).lower() for marker in BOT_CHECK_MARKERS):
            logger.error(
                "YouTube bot-check. POT_PROVIDER_URL=%r, cookies=%r",
                POT_PROVIDER_URL or None,
                COOKIES_PATH,
            )
            await status_msg.edit_text(
                "YouTube вимагає підтвердження, що запит не від бота — таке буває "
                "для запитів із дата-центру. Адміну варто перевірити сервіс "
                "PO token (POT_PROVIDER_URL)."
            )
        else:
            await status_msg.edit_text(f"Не вдалося завантажити аудіо: {e}")

    finally:
        # yt-dlp оставляет не только .mp3/.webm, но и .f140.*, .opus, .part —
        # поэтому чистим по маске, а не по списку расширений
        for path in glob.glob(out_path_no_ext + "*"):
            try:
                os.remove(path)
            except OSError:
                logger.warning("Не удалось удалить временный файл %s", path)


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
