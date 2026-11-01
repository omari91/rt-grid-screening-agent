# Stage 1: Wheel builder (compilers never ship in runtime image)
FROM python:3.11-slim AS builder

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src

RUN pip wheel --no-cache-dir --wheel-dir /wheels .

# Stage 2: Minimal non-root zero-trust runtime (<300 MB; zero compiler toolchain or network tools)
FROM python:3.11-slim AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

# 1. Install OpenMP runtime for OpenDSS, create UID 10001 gridagent, and purge network utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && (apt-get purge -y --auto-remove wget curl netcat-traditional netcat-openbsd 2>/dev/null || true) \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* /usr/bin/wget /usr/bin/curl /bin/nc* /usr/bin/nc* \
    && groupadd --gid 10001 gridagent \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin gridagent

WORKDIR /app
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels /wheels/*.whl \
    && rm -rf /wheels

# 2. Enforce strict root-owned directory permissions & purge package manager binaries
RUN mkdir -p /models \
    && cp -r /usr/local/lib/python3.11/site-packages/grid_screening_agent/fixtures/networks/* /models/ \
    && chown -R root:root /app /models \
    && chmod -R 755 /app /models \
    && (dpkg --purge --force-remove-essential apt 2>/dev/null || rm -rf /usr/bin/apt /usr/bin/apt-get /usr/bin/apt-cache)

# 3. Demote execution to unprivileged user (UID 10001)
USER gridagent
EXPOSE 8000

# Primary MCP SSE entrypoint (override with `grid-screen-api --host 0.0.0.0 --port 8000` for REST/Dashboard)
CMD ["grid-screen-mcp", "--transport", "sse", "--host", "0.0.0.0", "--port", "8000"]
