# mcp-corp

Implementación de referencia de un servidor MCP corporativo en Python. Este
repositorio es la **plantilla base** que se clonará para cada fuente de datos
concreta; por eso esta fase prioriza claridad y solidez del andamiaje sobre
velocidad de entrega.

**Estado actual: Fase 2 — capa de conectores.** Fase 1 (andamiaje base)
cerrada. Esta fase suma la infraestructura de resiliencia (concurrencia,
timeout, circuit breaker) y un conector real de PostgreSQL sobre esa
infraestructura. Todavía no hay tools MCP ni lógica de negocio: eso es la
Fase 3.

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
se está apagando. **Ninguno de los dos refleja la salud de los conectores**
(Postgres, APIs, etc.) — para eso está `/diagnostics` (ver más abajo).

## Cómo probar `/diagnostics`

```bash
curl -s http://localhost:8000/diagnostics | jq
```

Devuelve, por cada conector registrado, el estado del circuit breaker
(`closed` / `open` / `half_open`), operaciones en curso, el límite de
concurrencia configurado, el resultado de la última verificación de salud y
estadísticas crudas del pool. Ejemplo con el conector Postgres habilitado:

```json
{
  "connectors": {
    "postgres": {
      "source": "postgres",
      "circuit_state": "closed",
      "in_flight": 0,
      "max_concurrency": 10,
      "healthy": true,
      "pool": {"pool_size": 1, "pool_available": 1, "requests_num": 1, "...": "..."}
    }
  }
}
```

Este endpoint es solo para observabilidad/alertas manuales: a diferencia de
`/health` y `/ready`, nunca debe conectarse al healthcheck del orquestador
(ver "¿Por qué `/ready` nunca se acopla a los conectores?" más abajo).

## Cómo correrlo en Docker

```bash
docker build -t mcp-corp:latest .
docker run --rm -p 8000:8000 --env-file .env mcp-corp:latest
```

El `HEALTHCHECK` de la imagen apunta a `/health`. Build, arranque y
respuesta HTTP real (`curl /health` devolviendo `200` desde dentro del
contenedor) fueron verificados en un entorno con Docker Desktop.

## Cómo levantar Postgres de desarrollo (Fase 2)

Para probar el conector de Postgres localmente (fuera de los tests, que ya
lo hacen por su cuenta):

```bash
docker compose -f docker-compose.dev.yml up -d

# En tu .env:
# MCP_CORP_POSTGRES__ENABLED=true
# MCP_CORP_POSTGRES__DSN=postgresql://mcp_corp:mcp_corp@localhost:5432/mcp_corp

uv run python -m mcp_corp
curl -s http://localhost:8000/diagnostics | jq

docker compose -f docker-compose.dev.yml down -v
```

Ver [`docker-compose.dev.yml`](docker-compose.dev.yml) y los datos semilla
en [`deploy/dev/postgres-seed.sql`](deploy/dev/postgres-seed.sql). Esto es
solo para desarrollo local; no es el stack de despliegue (eso sigue siendo
`deploy/swarm/`).

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
├── main.py                    # entrypoint: python -m mcp_corp
├── server.py                  # FastMCP server: /health, /ready, /diagnostics, lifecycle
├── config.py                  # configuración vía pydantic-settings (incluye PostgresSettings)
├── logging_setup.py           # logging JSON estructurado a stdout
└── connectors/
    ├── base.py                # protocolo Connector: connect/close/health/run
    ├── resilience.py          # capa genérica: semáforo + timeout + circuit breaker
    ├── registry.py            # ciclo de vida y diagnóstico agregado de conectores
    └── postgres.py            # conector concreto: psycopg3 + psycopg_pool

tests/connectors/
├── test_resilience.py             # unitarios de la capa de resiliencia (conector falso)
└── test_postgres_integration.py   # integración contra Postgres real (docker-compose.dev.yml)

deploy/
├── swarm/              # docker-compose.yml para Docker Swarm/Portainer
├── dev/                 # postgres-seed.sql para docker-compose.dev.yml
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
migración dolorosa después.

**¿Por qué `/ready` nunca se acopla a la salud de los conectores?**
Corrección respecto a la Fase 1: el README original decía que `/ready` se
ampliaría para reflejar la salud de los conectores cuando existieran. Se
decidió lo contrario al construir la Fase 2. Razón: si el breaker de UNA
fuente se abre (p. ej. Postgres cae) y eso tumbara `/ready`, el balanceador
sacaría la réplica **entera** de rotación — incluidas las tools que no
dependen de esa fuente y que seguirían funcionando perfectamente. Eso
empeora el incidente en vez de contenerlo. `/ready` sigue siendo,
exclusivamente, "¿el proceso terminó de arrancar y no se está apagando?".
El estado de los conectores se expone aparte, en `/diagnostics`, pensado
para observabilidad y alertas — nunca para el healthcheck del orquestador.

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

### Fase 2 — capa de conectores

**¿Por qué dos capas separadas (resiliencia genérica + conectores concretos) y no un "conector Postgres" con su propia resiliencia adentro?**
Porque cuando sumemos la fuente número 20, 30 o 50, agregarla debe ser
"escribir cómo le hablo" y nada más. Si cada conector reimplementara su
semáforo, su timeout y su breaker, cada uno sería una superficie distinta
para el mismo bug, y arreglarlo en una fuente no arreglaría las demás. La
capa de resiliencia (`connectors/resilience.py`) no sabe nada de Postgres
ni de HTTP: solo envuelve cualquier objeto que cumpla el protocolo
`Connector` (`connectors/base.py`) — `connect`, `close`, `health`, `run`.
Un conector nuevo (REST, un sistema legacy) solo implementa esos cuatro
métodos y hereda concurrencia, timeout y circuit breaker gratis.

**¿Por qué psycopg3 (`psycopg[binary]==3.3.4` + `psycopg_pool==3.3.1`) y no asyncpg?**
Por compatibilidad con PgBouncer. Cuando escalemos réplicas, es previsible
que Postgres quede detrás de un PgBouncer en modo *transaction pooling*, y
ese modo no soporta *prepared statements* por conexión. asyncpg usa
prepared statements automáticamente y sin forma sencilla de desactivarlo
por completo, lo que produce `DuplicatePreparedStatementError` — un fallo
que **solo aparece bajo presión real del pool**, no en desarrollo ni en
tests ligeros, y que pasaría cualquier suite de pruebas hasta explotar en
producción. psycopg3 tiene un `prepare_threshold` adaptativo pensado
justamente para convivir con poolers externos. La diferencia de
rendimiento entre ambos drivers no es nuestro cuello de botella.

**¿Por qué circuit breaker propio y no una librería de terceros?**
Las semánticas (cerrado / abierto / medio-abierto, umbral de fallos, tiempo
de reset) son simples y acotadas — la implementación completa en
`resilience.py` no llega a 100 líneas. Las alternativas de PyPI para esto
suelen ser proyectos de un solo mantenedor. Este código corre en el camino
crítico de un sistema bancario: cada dependencia de terceros que no
sumamos es superficie de supply chain que no tenemos que auditar ni
defender. Código propio, tipado y con tests, es más barato de mantener que
una dependencia externa para algo de este tamaño.

**¿Por qué el estado del circuit breaker es por réplica (no compartido)?**
Decisión explícita, no un descuido: cada réplica mantiene su propio
`CircuitBreaker` en memoria de proceso. Cada réplica descubre una fuente
caída de forma independiente, con su propio conteo de fallos y su propio
reset. Coordinar el estado entre réplicas (p. ej. vía Redis) añadiría una
dependencia compartida más — y un punto de fallo más — para un beneficio
que en esta fase no justifica el costo. Queda como opción futura si la
detección coordinada se vuelve necesaria (p. ej. para no volver a golpear
una fuente que ya varias réplicas saben que está caída).

**¿Por qué `asyncio.BoundedSemaphore` (no `Semaphore`) y uno por fuente?**
`BoundedSemaphore` levanta `ValueError` si se hace `release()` de más que
`acquire()` — atrapa bugs de over-release en vez de corromper el contador
en silencio. Es uno por fuente, no global, para que el backpressure de
cada fuente sea independiente: si el core bancario se satura, eso no debe
robarle capacidad de espera a las consultas contra Postgres.

**¿Por qué `asyncio.timeout()` y no `asyncio.wait_for`?**
Es la API estructurada recomendada desde Python 3.11: compone mejor con
cancelación y no envuelve la corrutina en una `Task` adicional como hace
`wait_for`. Por la misma razón, si en el futuro hace falta ejecutar varias
operaciones concurrentes relacionadas, la herramienta es
`asyncio.TaskGroup`, no `asyncio.gather` — `gather` deja tareas huérfanas
corriendo si una de las otras falla; `TaskGroup` cancela las hermanas.

**Fórmula de capacidad: límite por réplica = techo de la fuente ÷ número de réplicas.**
`max_concurrency` (y, cuando exista, el límite de tasa) se configuran **por
réplica**, no en total. Si Postgres tolera 40 conexiones concurrentes desde
este servicio y corremos 4 réplicas, cada una debe configurarse con
`MCP_CORP_POSTGRES__MAX_CONCURRENCY=10` (40 ÷ 4), no con 40. Configurar 40
en cada una de las 4 réplicas permitiría hasta 160 conexiones simultáneas
en el peor caso y ahogaría la fuente exactamente cuando más tráfico hay.
Esta cuenta hay que rehacerla cada vez que cambia el número de réplicas.

**¿Por qué el semáforo no limita tasa (req/s), solo concurrencia?**
Hueco conocido, señalado a propósito y no resuelto en esta fase (ver
`rate_limit_per_second` en `ResilienceConfig` y en `PostgresSettings`, hoy
sin efecto). Un semáforo acota cuántas operaciones están *en vuelo* al
mismo tiempo, pero no cuántas *por segundo* se disparan — una fuente puede
declarar un techo en req/s en vez de en conexiones simultáneas (típico de
APIs REST con rate limiting). Si eso ocurre, hará falta un token bucket por
encima del semáforo; no existe todavía.

**¿Por qué `/diagnostics` es un endpoint separado de `/health` y `/ready`?**
Para poder observar y alertar sobre el estado de los conectores (breaker
abierto, pool agotado) sin que eso saque la réplica de rotación — ver
"¿Por qué `/ready` nunca se acopla a la salud de los conectores?" arriba.

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
resto de dependencias directas en `pyproject.toml` — incluidas
`psycopg[binary]==3.3.4` y `psycopg-pool==3.3.1` de la Fase 2 — y
`uv.lock` fija además todo el árbol transitivo para builds reproducibles.

**¿Por qué `pydantic-settings` para la configuración?**
Da validación de tipos y defaults declarativos sobre variables de entorno
con muy poco código, y es el enfoque estándar en el ecosistema FastMCP/
Pydantic. Toda variable nueva que se necesite en fases futuras se añade
como un campo más en `Settings`, reflejado también en `.env.example`.

## Próximas fases (fuera de alcance aquí)

- Fase 3+: tools MCP por intención de negocio sobre los conectores de la
  Fase 2, gateway de gobierno.
- Conectores adicionales (APIs REST, sistemas legacy) sobre el mismo
  protocolo `Connector` / `ResilientExecutor`.
- Token bucket para límite de tasa (req/s) por fuente — hoy solo hay
  límite de concurrencia (ver `rate_limit_per_second` en `config.py`).
- Estado del circuit breaker compartido entre réplicas (hoy es por
  réplica, a propósito — ver "Decisiones de diseño").
- Manifiestos de OpenShift/Kubernetes (`deploy/openshift/`).
