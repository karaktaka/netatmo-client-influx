# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:latest AS uv
FROM alpine

# Setup env
ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

COPY --from=uv /uv /usr/local/bin/

ARG UID=10001
RUN adduser -D -H -h /app -u "${UID}" appuser

USER appuser
WORKDIR /app

COPY --chown=${UID} pyproject.toml uv.lock src/* /app/
RUN --mount=type=cache,uid=${UID},target=/app/.cache \
    uv sync --frozen --no-dev

CMD [ "uv", "run", "client.py" ]
