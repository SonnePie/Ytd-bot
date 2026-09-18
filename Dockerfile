FROM python:3.12-slim

# ffmpeg is required by yt-dlp's FFmpegExtractAudio postprocessor
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

RUN useradd -m -u 1000 botuser \
    && mkdir -p /app/downloads \
    && chown -R botuser:botuser /app
USER botuser

# -u keeps logging unbuffered so `railway logs` shows output in real time
CMD ["python", "-u", "bot.py"]
