# syntax=docker/dockerfile:1
FROM python:3.12-slim AS builder

WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY pmex_shadow ./pmex_shadow
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 pmex \
    && useradd --uid 1000 --gid pmex --create-home --shell /usr/sbin/nologin pmex

COPY --from=builder /install /usr/local

WORKDIR /app
COPY alembic.ini ./
COPY alembic ./alembic
COPY pmex_shadow ./pmex_shadow

RUN mkdir -p /var/backups/pmex && chown -R pmex:pmex /app /var/backups/pmex

USER pmex

ENTRYPOINT ["pmex-shadow"]
CMD ["--help"]
