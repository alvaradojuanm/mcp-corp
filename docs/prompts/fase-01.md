# Fase 01 — Andamiaje base del server MCP corporativo

| Campo | Valor |
|---|---|
| **Fase** | 01 |
| **Objetivo** | Andamiaje base: estructura, config, logging, health/readiness, contenedor, deploy y Git. Sin tools ni conectores. |
| **Rama de trabajo** | `claude/mcp-corp-phase-1-scaffold-9j7iaj` → integrado en `main` |
| **Repo** | `github.com/alvaradojuanm/mcp-corp` |
| **Stack** | FastMCP 3.4.4 · Python 3.12+ · uv · Streamable HTTP |
| **Estado** | ✅ **CERRADA** |
| **Fecha de cierre** | 23 de julio de 2026 |
| **Versiones pineadas** | `fastmcp==3.4.4` · `uvicorn==0.35.0` |

---

## Resultado

Andamiaje completo, probado y documentado. `main` creada, empujada y establecida como rama por defecto. Historial con autoría 100 % `alvaradojuanm` vía correo noreply de GitHub.

### Cómo levantarlo

```bash
# Local con uv
uv sync --extra dev
cp .env.example .env
uv run python -m mcp_corp

# Docker
docker build -t mcp-corp:fase1 .
docker run --rm -p 8000:8000 --env-file .env mcp-corp:fase1

# Verificación
curl -i http://localhost:8000/health   # liveness
curl -i http://localhost:8000/ready    # readiness
```

Escucha en `0.0.0.0:8000`. Endpoint MCP (Streamable HTTP) en `/mcp`.

---

## Verificaciones de aceptación

- [x] `uv run` levanta el server en local
- [x] `/health` responde 200 (liveness)
- [x] `/ready` responde 200 (readiness); 503 durante arranque y apagado
- [x] SIGTERM produce apagado graceful (`shutdown_initiated` → `Application shutdown complete`)
- [x] Logging JSON estructurado a stdout, con `correlation_id` cableado vía `contextvars`
- [x] Endpoint `/mcp` responde a un handshake `initialize` real
- [x] **Contenedor construye** (validado con daemon real en máquina local)
- [x] **Contenedor arranca y responde** — `Up (healthy)`, `HTTP/1.1 200 OK` en `/health`
- [x] Corre como usuario no-root (`mcpcorp`, uid/gid 1000)
- [x] `.gitignore` verificado: sin `.env` ni secretos en el historial
- [x] `fastmcp` pineado a versión exacta
- [x] Autoría verificada: solo `alvaradojuanm`, sin coautorías ni firmas de herramienta
- [x] `main` empujada y establecida como rama por defecto
- [x] Auditoría de dependencias sin hallazgos

---

## Hallazgos: dos bugs del contenedor detectados en la verificación

El agente entregó el andamiaje de buena fe pero **no pudo verificar el contenedor** (su entorno tenía el CLI de Docker sin daemon). La validación en máquina local con Docker real destapó dos fallos. **Esta es la justificación del checklist de verificación.**

### Bug 1 — `OSError: Readme file does not exist: README.md`

- **Síntoma:** el build fallaba en `RUN uv sync --frozen --no-dev`.
- **Causa:** `pyproject.toml` declara `readme = "README.md"` en su metadata; hatchling lo necesita al instalar el paquete, pero el Dockerfile nunca copiaba el README al contenedor.
- **Fix:** añadir `README.md` al COPY existente → `COPY pyproject.toml uv.lock README.md ./`
  (mejor que un COPY aparte: queda antes del primer `uv sync` y no rompe el cacheo de capas).

### Bug 2 — `No module named mcp_corp` (el más grave)

- **Síntoma:** la imagen **construía sin error** pero el contenedor moría al arrancar. Bug silencioso: build en verde, falla en despliegue.
- **Causa:** `uv sync` instala en **modo editable** por defecto, dejando un `.pth` que apunta a `/app/src`. El runtime stage solo copia `/opt/venv`, no el código fuente — así que ese path no existe en la imagen final.
- **Fix:** `RUN uv sync --frozen --no-dev --no-editable`
  (superior a copiar `src/` al runtime: el paquete queda instalado de verdad dentro del venv y la imagen final no arrastra código fuente suelto).

**Commits:** `44968cd` (README) y `fa00de9` (`--no-editable`), ambos en `main`.

---

## Auditoría de dependencias

```bash
uv export --no-hashes --format requirements-txt > requirements-audit.txt
uvx pip-audit -r requirements-audit.txt
```

**Resultado: `No known vulnerabilities found`** — 77 dependencias de terceros auditadas (fastmcp, mcp, pydantic, uvicorn, httpx, cryptography, starlette, etc.).

Un paquete omitido: `mcp-corp==0.1.0`, el propio proyecto — no está en PyPI, omisión esperada.

> **Nota:** la auditoría es una foto del momento. Re-ejecutarla al cierre de cada fase, dado el ritmo de CVEs en este ecosistema. `requirements-audit.txt` es un artefacto derivado → va al `.gitignore`, no al repo (se regenera desde `uv.lock`, que sí está versionado).

---

## Decisiones tomadas por el agente

| Decisión | Veredicto |
|---|---|
| `fastmcp==3.4.4` en vez de 3.4.3 (regresión en el guard de Host/Origin) | ✅ Aprobada — criterio de seguridad correcto |
| `uvicorn==0.35.0` en vez de 0.34.0 | ✅ Forzada por resolución de dependencias, no fue elección |
| Añadir `__main__.py` (fuera de la lista exacta) | ✅ Aprobada — obligatorio para `python -m mcp_corp` |
| `uvicorn.Server` explícito en vez de `mcp.run()` | ✅ Aprobada — permite fijar `timeout_graceful_shutdown` desde config |
| Sin `LICENSE` | ✅ Aprobada — pendiente de definir licencia |
| Email `alvaradojuanm@gmail.com` | ❌ **Corregida** → noreply de GitHub |

### Corrección de autoría

El correo personal en el historial permanente de un repo corporativo no es aceptable. Se reescribió el historial completo con `git filter-repo` y force-push (`--force-with-lease`).

- **Correo final:** `114210637+alvaradojuanm@users.noreply.github.com`
- ID `114210637` verificado contra la API de GitHub.
- Los 6 commits (incluido el bootstrap) muestran solo `alvaradojuanm` en author y committer.
- Todos los hashes cambiaron por la reescritura — esperado.

### Restricción del entorno del agente

El harness solo permitía push a la rama designada, así que no pudo crear `main` ni `feat/estructura-base`. `main` se creó y empujó manualmente desde máquina local.

**Decisión de ramas revisada:** se descartó `feat/estructura-base`. Con un equipo de una persona más el agente, el modelo es **trunk-based simple**: el agente empuja a su rama del harness, se revisa, y se integra a `main`. Sin ramas de feature ni PRs por ahora. Ruleset de protección de `main` **no activado** — sería fricción sin beneficio en esta etapa; se evaluará cuando el repo pase a producción o lo toquen más manos.

---

## Lecciones para las fases siguientes

1. **Lo que el agente no puede verificar, se verifica en local.** Sus límites de entorno (sin daemon de Docker, sin Inspector) son puntos ciegos reales, no formalidades. Los dos bugs vivían exactamente ahí.
2. **"El build pasa" ≠ "funciona".** El bug 2 pasó el build limpio. Validar siempre que el contenedor *arranque y responda*, no solo que compile.
3. **Auditar dependencias en cada cierre de fase**, no solo al inicio.
4. **La política de autoría hay que declararla explícitamente** en cada prompt — el agente por defecto usa el correo que encuentre disponible.

---

## Prompt entregado a Claude Code

> El prompt íntegro está versionado en el repo como `docs/prompts/fase-01.md`. Resumen de sus secciones:

- **Contexto y norte:** FastMCP 3.x sobre Streamable HTTP, stateless para escalar por réplicas (Swarm/Traefik hoy, OpenShift después), diseño 12-factor. Aviso explícito de que conectores, tools y gateway son fases posteriores.
- **Instrucción de verificación previa:** consultar la documentación actual de FastMCP 3.x antes de escribir código (la 3.0 fue una reescritura mayor); preguntar en vez de inventar.
- **Alcance exacto:** estructura de carpetas cerrada — `pyproject.toml`, `.gitignore`, `.dockerignore`, `.env.example`, `README.md`, `Dockerfile`, `docs/prompts/`, `src/mcp_corp/` (server, config, logging_setup, connectors vacío), `tests/`, `deploy/swarm/` y `deploy/openshift/`.
- **Requisitos no negociables:** pydantic-settings sin nada hardcodeado; logging JSON con correlation id preparado; `/health` y `/ready` separados; graceful shutdown; contenedor no-root con HEALTHCHECK; compose con réplicas y labels de Traefik; README con sección de "Decisiones de diseño" como entregable.
- **Git:** identidad configurada antes de commitear y **política de autoría obligatoria** (solo `alvaradojuanm`, sin `Co-authored-by:` ni firmas de herramienta); nunca `.env` ni secretos; guardar copia del prompt en `docs/prompts/`.
- **Cierre:** verificar local, contenedor, Inspector y `git log`; reportar versión pineada, comandos de arranque y decisiones tomadas por cuenta propia; no avanzar a tools ni conectores.

---

## Siguiente

**Fase 02 — Capa de conectores:** un módulo autocontenido por fuente, con límite de concurrencia, timeout y circuit breaker. Sobre el molde ya probado.