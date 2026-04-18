# syntax=docker/dockerfile:1.6
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

FROM base AS build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --prefix=/install .

FROM base
COPY --from=build /install /usr/local
COPY config.example.toml ./config.example.toml
RUN useradd -r -u 1000 -m vibebot && mkdir -p /app/data && chown -R vibebot /app
USER vibebot
EXPOSE 8080
ENTRYPOINT ["vibebot"]
CMD ["serve", "--config", "/app/config.toml"]
