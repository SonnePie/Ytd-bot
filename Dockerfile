FROM python:3.12-slim

# ffmpeg — нужен постпроцессору FFmpegExtractAudio
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Node нужен для решения JS-challenge от YouTube: без него yt-dlp пишет
# "JS Challenge Providers: node (unavailable)" и запрос упирается в проверку
# "Sign in to confirm you're not a bot".
# Версия критична: yt-dlp требует node >= 23.5.0 (см. jsc/_builtin/node.py),
# а в Debian-репозитории только 20.x, который помечается как unsupported.
RUN curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && node --version

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Плагин PO token ставим отдельным слоем: сборка из кеша иначе может дать
# образ без плагина, и YouTube заблокирует запросы. Проверяем импортом —
# в реестр провайдеры попадают позже, при создании YoutubeDL, поэтому
# проверять список провайдеров на этапе сборки бессмысленно.
RUN pip install --no-cache-dir --force-reinstall bgutil-ytdlp-pot-provider \
    && python -c "import yt_dlp_plugins.extractor.getpot_bgutil_http as m; print('bgutil http provider:', m.__name__)" \
    && node --version

COPY bot.py .

RUN useradd -m -u 1000 botuser \
    && mkdir -p /app/downloads \
    && chown -R botuser:botuser /app
USER botuser

# -u keeps logging unbuffered so `railway logs` shows output in real time
CMD ["python", "-u", "bot.py"]
