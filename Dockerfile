# syntax=docker/dockerfile:1

# --- Etapa de build: resuelve e instala dependencias pineadas con uv ---
FROM python:3.12-slim AS builder

# Versión de uv pineada explícitamente (no "latest") para builds reproducibles.
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /usr/local/bin/uv

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# Copiamos solo los manifiestos primero para aprovechar la cache de capas.
# README.md se incluye porque pyproject.toml lo referencia como `readme` y
# hatchling lo exige al construir el paquete (ver builder 7/7 más abajo).
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

# Ahora sí el código fuente, e instalamos el propio paquete (sin dev deps).
# --no-editable: la imagen final solo copia /opt/venv, no /app/src, así que
# el paquete debe quedar instalado como copia real, no como enlace editable.
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# --- Etapa final: imagen slim, sin toolchain de build, usuario no-root ---
FROM python:3.12-slim AS runtime

# Requisito de seguridad: el proceso NUNCA corre como root.
RUN groupadd --system --gid 1000 mcpcorp \
    && useradd --system --uid 1000 --gid mcpcorp --no-create-home mcpcorp

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
USER mcpcorp

EXPOSE 8000

# El healthcheck de Docker/Swarm usa /health (liveness). Readiness (/ready)
# se consulta aparte por el orquestador cuando aplica (p. ej. OpenShift).
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)" || exit 1

ENTRYPOINT ["python", "-m", "mcp_corp"]
