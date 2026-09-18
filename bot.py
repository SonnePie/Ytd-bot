import asyncio
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

# Адрес локального Bot API Server (поднимается отдельно, см. README)
LOCAL_API_BASE_URL = os.getenv("LOCAL_API_BASE_URL", "http://localhost:8081")

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# На локальном Bot API Server лимит на отправку файлов — 2 ГБ вместо 50 МБ
MAX_FILESIZE_MB = 2000

YOUTUBE_URL_RE = re.compile(
    r"(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)[\w\-]+"
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

local_server = TelegramAPIServer.from_base(LOCAL_API_BASE_URL, is_local=True)
session = AiohttpSession(api=local_server)
bot = Bot(token=BOT_TOKEN, session=session)
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
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
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
        await status_msg.edit_text(f"Не вдалося завантажити аудіо: {e}")

    finally:
        # Убираем временные файлы
        for ext in (".mp3", ".webm", ".m4a", ".part"):
            path = out_path_no_ext + ext
            if os.path.exists(path):
                os.remove(path)


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
