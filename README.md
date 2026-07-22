# mcp-corp

Implementación de referencia de un servidor MCP corporativo en Python. Este
repositorio es la **plantilla base** que se clonará para cada fuente de datos
concreta; por eso esta fase prioriza claridad y solidez del andamiaje sobre
velocidad de entrega.

**Estado actual: Fase 1 — andamiaje base.** No hay tools, conectores ni
lógica de negocio todavía; solo la infraestructura sobre la que se
construirán en fases posteriores.

## Qué es esto

Un servidor [MCP](https://modelcontextprotocol.io/) construido con
[FastMCP](https://gofastmcp.com/) 3.x, expuesto sobre transporte
**Streamable HTTP**, diseñado para correr **stateless** y escalar
horizontalmente detrás de un balanceador — hoy Docker Swarm + Traefik
(Portainer), con OpenShift/Kubernetes como destino final.

## Cómo levantarlo en local con uv

Requisitos: Python 3.12+ y [uv](https://docs.astral.sh/uv/) instalado.

```bash
# Instala dependencias (incluye extras de desarrollo/test)
uv sync --extra dev

# Copia la config de ejemplo y ajústala si hace falta
cp .env.example .env

# Levanta el servidor
uv run python -m mcp_corp
```

Por defecto escucha en `http://0.0.0.0:8000`. El endpoint MCP
(Streamable HTTP) queda expuesto en `/mcp`.

## Cómo probar /health y /ready

```bash
curl -i http://localhost:8000/health   # liveness: el proceso está vivo
curl -i http://localhost:8000/ready    # readiness: listo para tráfico
```

Ambos responden `200` con un JSON `{"status": "...", "service": "mcp-corp"}`
cuando todo está bien; `/ready` responde `503` mientras el server arranca o
se está apagando.

## Cómo correrlo en Docker

```bash
docker build -t mcp-corp:latest .
docker run --rm -p 8000:8000 --env-file .env mcp-corp:latest
```

> Nota: la construcción y ejecución del contenedor no se pudo verificar en
> el entorno donde se desarrolló esta fase (no había daemon de Docker
> disponible, solo el CLI). El `Dockerfile` sí se revisó línea por línea y
> el server se probó exhaustivamente corriendo directamente con `uv run`;
> valida el build en tu máquina antes de confiar en él para producción.

El `HEALTHCHECK` de la imagen apunta a `/health`.

## Cómo desplegarlo en Docker Swarm

```bash
docker stack deploy -c deploy/swarm/docker-compose.yml mcp-corp
```

Ver los comentarios en [`deploy/swarm/docker-compose.yml`](deploy/swarm/docker-compose.yml)
para el detalle de las labels de Traefik y el healthcheck.

## Configuración

Toda la configuración viene de variables de entorno (prefijo `MCP_CORP_`),
cargadas con `pydantic-settings` en [`src/mcp_corp/config.py`](src/mcp_corp/config.py).
Ver [`.env.example`](.env.example) para la lista completa con valores de
ejemplo. **Nunca** commitees un `.env` real: está excluido en `.gitignore`.

## Estructura del proyecto

```
src/mcp_corp/
├── main.py            # entrypoint: python -m mcp_corp
├── server.py          # FastMCP server: rutas /health, /ready, lifecycle
├── config.py          # configuración vía pydantic-settings
├── logging_setup.py   # logging JSON estructurado a stdout
└── connectors/        # placeholder — se llena en la Fase 2

deploy/
├── swarm/              # docker-compose.yml para Docker Swarm/Portainer
└── openshift/           # placeholder — manifiestos K8s en otra fase
```

## Decisiones de diseño

**¿Por qué stateless por invocación?**
El proceso no guarda ningún estado de negocio en memoria ni en disco entre
requests. Esto permite correr N réplicas idénticas detrás de un balanceador
sin sticky sessions: cualquier réplica puede atender cualquier request. Es
la base para escalar horizontalmente en Swarm hoy y en OpenShift/Kubernetes
después, simplemente ajustando el número de réplicas.

**¿Por qué `/health` y `/ready` separados?**
Son preguntas distintas. `/health` (liveness) responde "¿el proceso sigue
vivo?" — si falla repetidamente, el orquestador debe reiniciar el
contenedor. `/ready` (readiness) responde "¿debo recibirle tráfico nuevo
ahora mismo?" — puede ser `false` durante el arranque o el apagado sin que
eso implique que el proceso está roto. Docker Swarm hoy solo usa un
healthcheck (mapeado a `/health`), pero OpenShift/Kubernetes exige sondas de
liveness y readiness independientes; separarlos desde ahora evita una
migración dolorosa después. En esta fase `/ready` solo refleja el estado del
propio proceso (arrancado / apagándose); cuando existan conectores en fases
futuras, `/ready` se ampliará para reflejar también su salud (p. ej. si un
circuit breaker está abierto).

**¿Por qué logging JSON estructurado a stdout, con correlation id?**
12-factor: el proceso no sabe ni le importa a dónde van sus logs; los emite
a stdout y el agregador de la plataforma (Docker/Portainer hoy, el stack de
logging de OpenShift después) los recolecta. El formato JSON los hace
parseables por esas plataformas sin heurísticas de texto libre. El
`correlation_id` (vía `contextvars`) se deja preparado desde ya en
`logging_setup.py` aunque todavía no hay tools que lo usen: la
infraestructura de auditoría de un server que va a manejar datos
corporativos debe existir desde el andamiaje, no añadirse como parche
cuando ya haya tráfico de negocio.

**¿Por qué usuario no-root en el contenedor?**
Requisito de seguridad estándar en entornos bancarios/corporativos: si el
proceso es comprometido, un usuario sin privilegios limita el radio de daño
dentro del contenedor. El `Dockerfile` crea un usuario de sistema dedicado
(`mcpcorp`, uid/gid 1000) y nunca ejecuta como root.

**¿Por qué apagado graceful ante SIGTERM controlando uvicorn explícitamente?**
`main.py` construye el `uvicorn.Server` directamente (en vez de usar
`mcp.run()`) para poder fijar `timeout_graceful_shutdown` desde la
configuración (`MCP_CORP_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS`). Uvicorn
instala los signal handlers: al recibir SIGTERM deja de aceptar conexiones
nuevas, espera a que las conexiones en curso terminen (hasta ese timeout) y
solo entonces dispara el shutdown del lifespan del server, donde marcamos
`ready = False` y lo dejamos registrado en el log. Esto se probó
manualmente enviando SIGTERM al proceso y confirmando en los logs la
secuencia `shutdown_initiated` → `Application shutdown complete`.

**¿Por qué `fastmcp` pineado a una versión exacta (`==3.4.4`)?**
FastMCP 3.0 fue una reescritura mayor de la librería; pinear la versión
exacta (en vez de un rango) evita que una actualización de terceros rompa
el comportamiento del server sin que nos demos cuenta. Lo mismo aplica al
resto de dependencias directas en `pyproject.toml`, y `uv.lock` fija
además todo el árbol transitivo para builds reproducibles.

**¿Por qué `pydantic-settings` para la configuración?**
Da validación de tipos y defaults declarativos sobre variables de entorno
con muy poco código, y es el enfoque estándar en el ecosistema FastMCP/
Pydantic. Toda variable nueva que se necesite en fases futuras se añade
como un campo más en `Settings`, reflejado también en `.env.example`.

## Próximas fases (fuera de alcance aquí)

- Fase 2: capa de conectores (pool de conexiones, timeout y circuit
  breaker por fuente de datos).
- Fase 3+: tools por intención de negocio, gateway de gobierno.
- Manifiestos de OpenShift/Kubernetes (`deploy/openshift/`).
