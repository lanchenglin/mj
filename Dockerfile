FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fontconfig fonts-noto-cjk && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY mj ./mj
RUN pip install --no-cache-dir '.[postgres]' && useradd --uid 10001 --create-home mj && mkdir -p /data && chown mj:mj /data
COPY alembic.ini ./
COPY alembic ./alembic
USER mj
EXPOSE 8080
CMD ["python", "-m", "mj.cli", "serve", "--host", "0.0.0.0"]
