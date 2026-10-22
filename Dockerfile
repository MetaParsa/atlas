# Torob in-store agent — Liara Docker platform.
# Liara mirrors: Docker Hub and PyPI can be blocked from Iranian build servers (docs/liara-research.md #22, #23).
ARG BASE=docker-mirror.liara.ir/library/python:3.12-slim
FROM ${BASE}

ARG PIP_INDEX=https://package-mirror.liara.ir/repository/pypi/simple
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    TOROB_DB=/data/torob.db TOROB_APP_DB=/data/app.db TRUST_PROXY=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --index-url ${PIP_INDEX} -r requirements.txt
COPY core ./core
COPY torob ./torob
RUN rm -rf torob/tests

EXPOSE 8000
CMD ["uvicorn", "torob.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
