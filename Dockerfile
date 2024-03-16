# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-alpine as base

# Setup env
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONFAULTHANDLER=1
ENV PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:$PATH"

WORKDIR /app


FROM base AS venv

ARG categories="packages"

# Install pipenv and compilation dependencies
RUN pip install pipenv
ADD Pipfile.lock Pipfile /app/
RUN PIPENV_VENV_IN_PROJECT=1 pipenv install --deploy --categories ${categories}


FROM base as app

ARG UID=10001
RUN adduser \
    --disabled-password \
    --gecos "" \
    --home "/nonexistent" \
    --shell "/sbin/nologin" \
    --no-create-home \
    --uid "${UID}" \
    appuser

USER appuser

COPY --from=venv /app/.venv /app/.venv
COPY netatmo-client/netatmo_influx.py /app/

CMD [ "python", "netatmo_influx.py" ]
