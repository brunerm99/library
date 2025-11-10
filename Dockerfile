# syntax=docker/dockerfile:1

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=on \
    UV_SYSTEM_PYTHON=1

WORKDIR /app

# Optional: poppler-utils for PDF text extraction
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv (fast Python packaging)
RUN pip install --no-cache-dir uv

# Copy project
COPY pyproject.toml README.md ./
COPY libindex ./libindex

# Bundle offline viewer libraries (pdf.js, epub.js)
RUN set -eux; \
    mkdir -p libindex/web/vendor/pdfjs libindex/web/vendor/epub; \
    curl -fsSL https://cdn.jsdelivr.net/npm/epubjs@0.3.93/dist/epub.min.js -o libindex/web/vendor/epub/epub.min.js; \
    curl -fsSL https://cdn.jsdelivr.net/npm/jszip@3.10.1/dist/jszip.min.js -o libindex/web/vendor/epub/jszip.min.js; \
    curl -fsSL https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js -o libindex/web/vendor/pdfjs/pdf.min.js; \
    curl -fsSL https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js -o libindex/web/vendor/pdfjs/pdf.worker.min.js

# Install as editable package (no external deps, but keeps CLI entrypoint)
RUN uv pip install --system -e .

# Default ports and volumes
EXPOSE 8080
VOLUME ["/data", "/content"]

# By default, use /data for db/config; run init+serve
WORKDIR /data

ENV LIBRARY_ROOTS=/content \
    LIBINDEX_AUTOSCAN=1

CMD ["python", "-m", "libindex.cli", "serve", "--host", "0.0.0.0", "--port", "8080"]
