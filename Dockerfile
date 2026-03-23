FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_RUNTIME_DIR=/app/runtime

WORKDIR /app

RUN groupadd --system app && useradd --system --gid app --create-home --home-dir /home/app app

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY main.py README.md LICENSE ./
COPY templates ./templates

RUN mkdir -p /app/database /app/runtime \
    && chown -R app:app /app

USER app

EXPOSE 5000

CMD ["python", "main.py"]
