# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-alpine AS base

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


FROM base AS app

ARG UID=10001
RUN adduser -D -H -h /app -u "${UID}" appuser

USER appuser

COPY --from=venv --chown=${UID} /app/.venv /app/.venv
COPY --chown=${UID} netatmo-client/netatmo_influx.py /app/

CMD [ "python", "netatmo_influx.py" ]
