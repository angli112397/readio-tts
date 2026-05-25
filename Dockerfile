FROM python:3.12-slim@sha256:090ba77e2958f6af52a5341f788b50b032dd4ca28377d2893dcf1ecbdfdfe203

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install .

RUN addgroup --system readio \
    && adduser --system --ingroup readio --home /app readio \
    && mkdir -p /app/data/jobs \
    && chown -R readio:readio /app

USER readio

EXPOSE 8090

CMD ["uvicorn", "readio_tts.api:app", "--host", "0.0.0.0", "--port", "8090", "--workers", "1"]
