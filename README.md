# mcp-corp

Implementación de referencia de un servidor MCP corporativo en Python. Este
repositorio es la **plantilla base** que se clonará para cada fuente de datos
concreta; por eso esta fase prioriza claridad y solidez del andamiaje sobre
velocidad de entrega.

**Estado actual: Fase 3 — conector HTTP y primeras tools MCP.** Fases 1
(andamiaje base) y 2 (capa de conectores + Postgres) cerradas. Esta fase
suma un segundo conector, de naturaleza distinta (API REST vía HTTP,
sobre la misma capa de resiliencia genérica de la Fase 2), y las primeras
tools de negocio, un Resource y un Prompt — el camino completo
agente → tool → conector → dato ya funciona de punta a punta.

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

## Cómo levantar la infraestructura de desarrollo (Postgres + stub de saldos)

Para probar los conectores de las Fases 2 y 3 localmente (fuera de los
tests de integración, que ya lo hacen por su cuenta):

```bash
docker compose -f docker-compose.dev.yml up -d

# En tu .env:
# MCP_CORP_POSTGRES__ENABLED=true
# MCP_CORP_POSTGRES__DSN=postgresql://mcp_corp:mcp_corp@localhost:5432/mcp_corp
# MCP_CORP_SALDO_API__ENABLED=true
# MCP_CORP_SALDO_API__BASE_URL=http://localhost:8080

uv run python -m mcp_corp
curl -s http://localhost:8000/diagnostics | jq

docker compose -f docker-compose.dev.yml down -v
```

`docker compose ... down -v` es importante si cambiaste el seed: Postgres
solo corre `docker-entrypoint-initdb.d` en un volumen de datos vacío, así
que sin el `-v` una recreación del contenedor NO vuelve a sembrar la base
(la tabla `clientes` nueva de esta fase no aparecía hasta hacer el `-v`).

Ver [`docker-compose.dev.yml`](docker-compose.dev.yml), el seed de
Postgres en [`deploy/dev/postgres-seed.sql`](deploy/dev/postgres-seed.sql)
y el stub de saldos en
[`deploy/dev/saldo_api_stub.py`](deploy/dev/saldo_api_stub.py). Esto es
solo para desarrollo local; no es el stack de despliegue (eso sigue siendo
`deploy/swarm/`).

## Tools, Resource y Prompt (Fase 3)

Con la infraestructura de desarrollo arriba (Postgres + stub de saldos) y
el server corriendo con ambos conectores habilitados, cualquier cliente
MCP puede invocar lo siguiente contra `http://localhost:8000/mcp`:

### Las tres tools

| Tool | Fuente | Parámetro | Devuelve |
|---|---|---|---|
| `consultar_cliente` | PostgreSQL (`clientes`) | `cedula: str` (6-10 dígitos) | `{cedula, nombre, email, estado}` |
| `consultar_saldo` | API REST de saldos (stub) | `cedula: str` (6-10 dígitos) | `{cedula, saldo, moneda}` |
| `resumen_cliente` | ambas, en paralelo | `cedula: str` (6-10 dígitos) | ver "Política de resultado parcial" abajo |

`consultar_cliente` y `consultar_saldo` fallan limpio (`ToolError`, mensaje
de negocio) si la cédula no existe o si su fuente no está disponible.
`resumen_cliente` es la tool compuesta preferida cuando se necesitan ambos
datos: una sola llamada, ambas fuentes consultadas en paralelo con
`asyncio.TaskGroup`.

Cédulas de prueba (ver el seed): `1000000001` y `1000000002` existen en
ambas fuentes (caso feliz); `1000000003` existe solo en Postgres (para
probar el resultado parcial); `5555555555` existe en Postgres y hace que
el stub de saldos responda `500` (para probar el circuit breaker con un
fallo real de infraestructura, no un simple "no encontrado").

### Política de resultado parcial de `resumen_cliente`

Si una de las dos fuentes no está disponible (circuito abierto, timeout,
fallo de infraestructura), la tool **no falla entera**: devuelve lo que sí
pudo obtener y marca explícitamente qué falta y por qué.

```json
{
  "cedula": "1000000003",
  "cliente": {"disponible": true, "datos": {"...": "..."}, "motivo": null},
  "saldo": {"disponible": false, "datos": null, "motivo": "el servicio de saldos no está disponible en este momento"},
  "resumen_completo": false
}
```

Un modelo que consuma esta tool debe revisar siempre `cliente.disponible`
/ `saldo.disponible` antes de usar `datos` — un dato ausente NUNCA es cero
ni un valor válido. El Prompt `atencion_cliente` (ver abajo) codifica esta
regla explícitamente para el flujo de atención al cliente.

### Resource: catálogo de estados de cliente

`data://clientes/estados` — un diccionario JSON de solo lectura con el
significado de cada código de `estado` (`activo`, `moroso`, `inactivo`)
que puede traer `consultar_cliente` / `resumen_cliente`.

**Diferencia entre Tool y Resource:** una Tool es una ACCIÓN que el modelo
decide invocar (con efectos — en este caso, consultar una fuente externa
en el momento). Un Resource es CONTEXTO de solo lectura que la aplicación
cliente puede cargar sin que el modelo gaste un turno de tool-call — p.
ej. para mostrarlo en una UI, o para que el modelo lo tenga ya disponible
al interpretar el campo `estado` de un cliente.

### Prompt: flujo de atención al cliente

`atencion_cliente(cedula)` — una plantilla de workflow reutilizable que le
dice al modelo, en orden: usar `resumen_cliente` primero, revisar los
campos `disponible` antes de reportar nada, no inventar datos faltantes, y
cuándo usar las tools individuales o el Resource de estados en su lugar.

### Probar todo de punta a punta

```bash
docker compose -f docker-compose.dev.yml up -d
# .env con MCP_CORP_POSTGRES__ENABLED=true y MCP_CORP_SALDO_API__ENABLED=true
uv run python -m mcp_corp
```

Con el server corriendo, usando el cliente de `fastmcp` (incluido como
dependencia transitiva):

```python
import asyncio
from fastmcp import Client

async def main():
    async with Client("http://localhost:8000/mcp") as client:
        print(await client.list_tools())
        print(await client.call_tool("resumen_cliente", {"cedula": "1000000001"}))

asyncio.run(main())
```

O ejecuta directamente `uv run pytest tests/tools/` (unitarios sin
infraestructura + integración contra el stack real, se saltan solos si no
está levantado).

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
├── server.py                  # FastMCP server: /health, /ready, /diagnostics, tools/resource/prompt, lifecycle
├── config.py                  # configuración vía pydantic-settings (Postgres + saldo_api)
├── logging_setup.py           # logging JSON estructurado a stdout
├── audit.py                   # auditoría por invocación de tool (correlation id + enmascaramiento)
├── tools.py                   # las 3 tools de negocio + Resource + Prompt (Fase 3)
└── connectors/
    ├── base.py                # protocolo Connector: connect/close/health/run
    ├── resilience.py          # capa genérica: semáforo + timeout + circuit breaker
    ├── registry.py            # ciclo de vida y diagnóstico agregado de conectores
    ├── postgres.py            # conector concreto: psycopg3 + psycopg_pool
    └── http.py                 # conector concreto: httpx.AsyncClient (Fase 3)

tests/
├── test_audit.py                       # unitarios de auditoría: enmascaramiento, forma del log
├── connectors/
│   ├── test_resilience.py              # unitarios de la capa de resiliencia (conector falso)
│   ├── test_postgres_integration.py    # integración contra Postgres real
│   └── test_http.py                    # unitarios del conector HTTP (transporte falso)
└── tools/
    ├── test_tools_logic.py             # unitarios de las tools: parcial, fuente caída, sin internals
    └── test_tools_integration.py       # integración contra Postgres + stub de saldos reales

deploy/
├── swarm/                 # docker-compose.yml para Docker Swarm/Portainer
├── dev/                    # postgres-seed.sql + saldo_api_stub.py para docker-compose.dev.yml
└── openshift/              # placeholder — manifiestos K8s en otra fase
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

### Fase 3 — conector HTTP y tools de negocio

**El conector HTTP valida la abstracción de la Fase 2 contra algo que no es Postgres.**
`connectors/http.py` implementa el mismo protocolo `Connector` (`connect`,
`close`, `health`, `run`) y se envuelve con el mismo `ResilientExecutor`,
sin que la capa de resiliencia sepa que esta vez el recurso subyacente es
un `httpx.AsyncClient` en vez de un pool de psycopg. No hizo falta tocar
`resilience.py` ni `base.py` para nada: la abstracción de la Fase 2 no
resultó forzada — de hecho salió MÁS simple que el conector de Postgres,
porque no hay un pool que administrar a mano (`httpx.AsyncClient` ya trae
el suyo internamente). Eso es justamente la señal de que el diseño de la
Fase 2 estaba bien encontrado, no una casualidad.

**¿Por qué `httpx==0.28.1` como dependencia directa (y no solo transitiva)?**
Ya era una dependencia transitiva de `fastmcp`/`mcp` desde la Fase 1 (por
eso la versión coincide exactamente); pasa a dependencia directa en esta
fase porque `connectors/http.py` la usa en tiempo de ejecución, no solo en
tests. Se quita de `dev` en `pyproject.toml` para no declararla dos veces.

**¿Qué cuenta como fallo de infraestructura para una fuente HTTP?**
`HTTP_INFRA_EXCEPTIONS = (httpx.TransportError, httpx.HTTPStatusError)` en
`connectors/http.py`. `TransportError` cubre problemas reales de red/
conexión/timeout de transporte. `HTTPStatusError` solo se levanta cuando
el código de la tool llama a `response.raise_for_status()` — y las tools
de esta fase manejan el `404` ("cédula no encontrada") como caso de
NEGOCIO antes de llegar ahí, devolviendo `None` en vez de lanzar. Así que
lo que efectivamente cuenta como fallo de infraestructura es un `5xx` real
del servicio, no un simple "no encontrado".

**Tools por intención de negocio, no una por endpoint.**
Tres tools (`consultar_cliente`, `consultar_saldo`, `resumen_cliente`), no
una por cada endpoint de cada fuente. Un server con demasiadas tools
parecidas degrada la capacidad del modelo de elegir bien cuál usar — este
es el motivo documentado de que se evitó deliberadamente una tool por
endpoint. Las descripciones de cada tool son la interfaz real que lee el
modelo: dicen explícitamente cuándo usar esa tool y cuándo usar otra en su
lugar (p. ej. "si también necesitas el saldo, usa `resumen_cliente` en su
lugar").

**Resultado parcial explícito en `resumen_cliente`, nunca fallo total.**
Ver "Política de resultado parcial" arriba. La razón de fondo: un modelo
que recibe un error genérico cuando una de dos fuentes falla no puede
decirle al usuario "tengo el dato del cliente pero no el saldo ahora
mismo" — se queda sin nada que podía haber tenido. Ambas ramas de
`_resumen_cliente_logic` atrapan sus propios `ConnectorError` (nunca dejan
escapar una excepción "esperada"); solo un bug real y no contemplado
propagaría una excepción fuera de la tool, y ahí sí queremos que falle
fuerte en vez de fingir un resultado parcial.

**¿Por qué `asyncio.TaskGroup` y no `asyncio.gather` en la tool compuesta?**
Misma razón que en la Fase 2: `gather` deja tareas huérfanas corriendo si
una falla; `TaskGroup` las cancela. Aquí, además, ninguna de las dos tareas
internas deja escapar una excepción esperada (la atrapan y la traducen a
`disponible=False`), así que `TaskGroup` casi nunca tiene que cancelar
nada en la práctica — pero es la primitiva correcta igual, para el día en
que sí aparezca un bug real que deba cancelar a la tarea hermana.

**Errores hacia el modelo: `ToolError` + `mask_error_details=True`, dos capas.**
Cada tool atrapa `ConnectorError` (ya sanitizado por `resilience.py`: su
mensaje nunca incluye el DSN, el SQL ni el string de la excepción
original) y lo traduce a un `ToolError` con un mensaje de negocio propio,
escrito a mano — nunca `str(excepción_original)`. Como defensa en
profundidad adicional, el server se crea con
`FastMCP(..., mask_error_details=True)`: cualquier excepción que NO sea un
`ToolError` (es decir, un bug no contemplado) se enmascara automáticamente
hacia el cliente en vez de reenviar el traceback completo.

**Criterio de enmascaramiento en el log de auditoría (`audit.py`).**
Cada invocación de tool queda en el log JSON con: `correlation_id` (nuevo
por invocación, reutilizando el mecanismo que ya existía desde la Fase 1),
nombre de la tool, `duration_ms`, y `result` (`success` / `partial` /
`failure`, con `reason` si falló). Lo que NUNCA se registra es el VALOR de
los parámetros de negocio: una cédula, nombre o saldo en claro en un log
que viaja a un agregador externo es, en sí mismo, un problema de
cumplimiento. Lo único que se conserva del identificador principal (la
cédula) es `HMAC-SHA256(clave, cédula)` truncado a 12 hex — permite
correlacionar invocaciones del mismo cliente entre líneas de log sin poder
recuperar el valor original a partir del log.

**¿Por qué HMAC-SHA256 y no un `sha256(cédula)` plano?**
Un hash plano NO es irreversible en este caso concreto: el espacio de
cédulas (6 a 10 dígitos) es pequeño y enumerable — calcular `sha256` de
los ~10 mil millones de valores posibles y armar una tabla arcoíris toma
segundos en cualquier laptop. Cualquiera con acceso al log (el agregador
externo, un auditor, un atacante que lo filtre) podría revertir el
identificador sin necesitar ningún secreto — el "enmascaramiento" no
protegería nada. `HMAC(clave, cédula)` corta ese ataque: sin conocer
`audit_hmac_secret` (nueva variable en `.env.example`, nunca un valor real
en el repo), ni siquiera se puede empezar a precomputar la tabla, porque
el HMAC de cada cédula depende de una clave que no está en el log.
Sigue siendo determinista (misma cédula + misma clave = mismo hash, ver
`_mask` en `audit.py`) y por lo tanto correlacionable — pero deja de ser
reversible por fuerza bruta desde fuera del server.

**Qué pasa con la correlación histórica si `audit_hmac_secret` rota.**
`HMAC(clave_nueva, cédula) ≠ HMAC(clave_vieja, cédula)` para la misma
cédula — es el comportamiento esperado, no un defecto. Rotar la clave
rompe la correlación entre logs de ANTES y DESPUÉS de la rotación para el
mismo cliente: dos invocaciones de la misma cédula, una a cada lado de la
rotación, quedan con hashes distintos y no se pueden enlazar mirando solo
el log. Es exactamente el trade-off deseable al rotar por sospecha de
compromiso de la clave: invalida la posibilidad de correlacionar hacia
atrás usando la clave filtrada. Si en el futuro se necesita continuidad de
correlación durante una rotación planificada (no por incidente), la única
forma es calcular el hash con AMBAS claves durante una ventana de
transición — no implementado en esta fase.

**Clave igual en todas las réplicas, no por-réplica.**
A diferencia del estado del circuit breaker (que sí es por réplica, ver
Fase 2), `audit_hmac_secret` debe ser IDÉNTICA en todas las réplicas: si
cada una tuviera su propia clave, la misma cédula produciría hashes
distintos según qué réplica atendió la invocación, y dejarías de poder
correlacionar al mismo cliente entre logs de réplicas diferentes — el
caso de uso exactamente contrario al del breaker.

**Hallazgo importante: el `JSONFormatter` de la Fase 1 ignoraba `extra={}`.**
Al validar el log de auditoría de punta a punta se descubrió que
`logging_setup.JSONFormatter` (desde la Fase 1) solo incluía
`correlation_id` en el JSON — cualquier otro campo pasado vía
`logger.info(msg, extra={...})` se registraba en el `LogRecord` pero
JAMÁS se escribía en la línea de log final. Esto afectaba TODOS los logs
estructurados desde la Fase 2 (`source`, `circuit_state`, etc.), no solo
los de esta fase; se corrigió volcando cualquier atributo del `LogRecord`
que no sea uno de los estándar de `logging`. Verificado manualmente
comparando el log antes/después del fix contra el mismo flujo de tools.

**Prompt y Resource: mismo patrón de registro que las tools, sin resiliencia.**
Ninguno de los dos toca una fuente externa en el momento (el Resource es
un diccionario estático en memoria; el Prompt es una plantilla de texto),
así que no pasan por `ResilientExecutor` — no tienen de qué protegerse.

## Próximas fases (fuera de alcance aquí)

- Fase 4+: gateway de gobierno, más tools de negocio, más conectores
  (sistemas legacy) sobre el mismo protocolo `Connector` / `ResilientExecutor`.
- Token bucket para límite de tasa (req/s) por fuente — hoy solo hay
  límite de concurrencia (ver `rate_limit_per_second` en `config.py`).
- Estado del circuit breaker compartido entre réplicas (hoy es por
  réplica, a propósito — ver "Decisiones de diseño").
- Manifiestos de OpenShift/Kubernetes (`deploy/openshift/`).
