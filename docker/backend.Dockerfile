FROM python:3.12-slim AS backend

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY law_agent ./law_agent
COPY alembic.ini ./
COPY alembic ./alembic
COPY docker/fonts/NotoSansSC-Regular.ttf /usr/local/share/fonts/NotoSansSC-Regular.ttf
COPY docker/fonts/NotoSansSC-SemiBold.ttf /usr/local/share/fonts/NotoSansSC-SemiBold.ttf

RUN python -m pip install --no-cache-dir \
      ".[service]" \
      "alembic>=1.13" \
      "SQLAlchemy>=2.0" \
      "minio>=7.2" \
      "reportlab>=4.0" \
      "httpx>=0.27"

RUN useradd --create-home --uid 10001 crosscomply \
    && mkdir -p /app/data/corpus/legal_docs_20260702 \
    && chown -R crosscomply:crosscomply /app

USER crosscomply

CMD ["uvicorn", "law_agent.review.api:app", "--host", "0.0.0.0", "--port", "8000"]

FROM backend AS review-worker
USER root
ADD --checksum=sha256:8de7834105edef8db8a13299e2587cf8945021dd8764f2691d83bbc25129dfa2 https://github.com/brettcannon/cpython-wasi-build/releases/download/v3.14.9/python-3.14.9-wasi_sdk-24.zip /tmp/python-wasi.zip
RUN python -m zipfile -e /tmp/python-wasi.zip /opt/python-wasi \
    && rm /tmp/python-wasi.zip \
    && python -m pip install --no-cache-dir ".[sandbox]"
ENV LAWAGENT_WASI_PYTHON_ROOT=/opt/python-wasi
USER crosscomply

FROM backend
