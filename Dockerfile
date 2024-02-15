FROM python:3.11-alpine

ENV POETRY_VIRTUALENVS_IN_PROJECT=true

COPY pyproject.toml poetry.lock README.md /app/
COPY netatmo-client/netatmo_influx.py /app/netatmo-client/

WORKDIR /app
RUN apk add --no-cache build-base libffi-dev \
    && python3 -m pip install --no-cache-dir --trusted-host pypi.python.org poetry==1.7.1 \
    && poetry install --no-interaction --no-ansi --without dev \
    && apk del build-base libffi-dev \
    && rm -rf /var/cache/apk/* ~/.cache/pypoetry ~/.local/share/virtualenv

CMD [ "poetry", "run", "python", "/app/netatmo-client/netatmo_influx.py" ]
