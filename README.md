# mcp-corp

Implementación de referencia de un servidor MCP corporativo en Python. Este
repositorio es la **plantilla base** que se clonará para cada fuente de datos
concreta; por eso esta fase prioriza claridad y solidez del andamiaje sobre
velocidad de entrega.

**Estado actual: Fase 4 — identificadores venezolanos + despliegue y escalado.**
Fases 1 (andamiaje base), 2 (capa de conectores + Postgres) y 3 (conector
HTTP + tools MCP + auditoría) cerradas. La Fase 4 tuvo dos partes: la
Parte A normaliza y valida cédulas/RIF venezolanos (`identifiers.py`) para
que las tools acepten el formato que de verdad escribe un usuario; la
Parte B verifica bajo carga real la tesis central del diseño — más
réplicas atienden más tráfico sin tocar el código — y deja el fail-closed
del HMAC de auditoría en modo producción.

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

## Tools, Resource y Prompt (Fases 3 y 4)

Con la infraestructura de desarrollo arriba (Postgres + stub de saldos) y
el server corriendo con ambos conectores habilitados, cualquier cliente
MCP puede invocar lo siguiente contra `http://localhost:8000/mcp`:

### Las tres tools

| Tool | Fuente | Parámetro | Devuelve |
|---|---|---|---|
| `consultar_cliente` | PostgreSQL (`clientes`) | `identificador: str` (ver formatos abajo) | `{cedula, nombre, email, estado}` |
| `consultar_saldo` | API REST de saldos (stub) | `identificador: str` | `{cedula, saldo, moneda}` |
| `resumen_cliente` | ambas, en paralelo | `identificador: str` | ver "Política de resultado parcial" abajo |

`consultar_cliente` y `consultar_saldo` fallan limpio (`ToolError`, mensaje
de negocio) si el identificador no existe o si su fuente no está
disponible. `resumen_cliente` es la tool compuesta preferida cuando se
necesitan ambos datos: una sola llamada, ambas fuentes consultadas en
paralelo con `asyncio.TaskGroup`.

Identificadores de prueba (ver el seed): `V16760320` y `V16760321` existen
en ambas fuentes (caso feliz) — pueden escribirse también como
`16760320`, `16.760.320`, `V-16.760.320`, etc. (ver "Identificadores
venezolanos" abajo); `V16760322` existe solo en Postgres (para probar el
resultado parcial); `V90000001` existe en Postgres y hace que el stub de
saldos responda `500` (para probar el circuit breaker con un fallo real
de infraestructura, no un simple "no encontrado").

### Identificadores venezolanos: formatos aceptados (Fase 4)

Las tres tools aceptan cédula o RIF venezolano en cualquier formato común
— la normalización vive en [`identifiers.py`](src/mcp_corp/identifiers.py),
no en el esquema de la tool ni en el modelo. El modelo no necesita limpiar
la entrada del usuario antes de pasarla.

**Formatos equivalentes** (todos normalizan al mismo valor canónico):

```
Cédula:  16760320      16.760.320      V16760320
         V-16760320    V-16.760.320    v16760320
RIF:     J-167603200   J16.760.320-0   J-16760320-0
```

Con o sin puntos de millar, con o sin guiones, con o sin letra de
prefijo, mayúscula o minúscula.

**Prefijos aceptados: `V`, `E`, `J`, `G`, `P`** — verificados contra
fuentes (ver "Decisiones de diseño" para el detalle y las fuentes
consultadas). `C` (comunas/consejos comunales) existe pero es ambiguo
entre fuentes; queda deshabilitado por defecto, activable con
`MCP_CORP_IDENTIFIERS__INCLUIR_PREFIJO_C=true`.

**La letra `I` NO es un prefijo válido** y se rechaza explícitamente —
varias librerías y regex publicados la incluyen por error; el registro
oficial del SENIAT no la contempla.

**Dígito verificador del RIF:** cuando el identificador trae uno (un RIF
completo, no solo una cédula), se valida contra la fórmula módulo 11 del
SENIAT ANTES de tocar cualquier conector — un identificador mal tipeado
se rechaza sin gastar un slot del semáforo ni abrir una conexión.
Desactivable con `MCP_CORP_IDENTIFIERS__VALIDAR_DIGITO_VERIFICADOR=false`
si alguna vez hiciera falta (ver "Decisiones de diseño" para cómo se
verificó el algoritmo).

Un identificador con formato irreconocible, con un prefijo no válido, o
(si la validación está activa) con un dígito verificador que no coincide,
produce un `ToolError` de negocio ("el identificador no tiene un formato
reconocido" / "el dígito verificador no es válido") — nunca un error
técnico, y nunca después de haber tocado Postgres o la API de saldos.

### Política de resultado parcial de `resumen_cliente`

Si una de las dos fuentes no está disponible (circuito abierto, timeout,
fallo de infraestructura), la tool **no falla entera**: devuelve lo que sí
pudo obtener y marca explícitamente qué falta y por qué.

```json
{
  "identificador": "V16760322",
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
        print(await client.call_tool("resumen_cliente", {"identificador": "V-16.760.320"}))

asyncio.run(main())
```

O ejecuta directamente `uv run pytest tests/tools/` (unitarios sin
infraestructura + integración contra el stack real, se saltan solos si no
está levantado).

## Cómo desplegarlo en Docker Swarm

```bash
# Obligatorio: MCP_CORP_ENVIRONMENT=production (ya fijado en el compose)
# exige MCP_CORP_AUDIT_HMAC_SECRET, fail-closed desde la Fase 4 — ver
# "Modo producción y fail-closed del HMAC" más abajo.
export MCP_CORP_AUDIT_HMAC_SECRET="$(python -c 'import secrets; print(secrets.token_hex(32))')"
docker stack deploy -c deploy/swarm/docker-compose.yml mcp-corp
```

Ver los comentarios en [`deploy/swarm/docker-compose.yml`](deploy/swarm/docker-compose.yml)
para el detalle de las labels de Traefik, el healthcheck, y cómo escalar
réplicas desde Portainer.

## Cómo desplegarlo en OpenShift / Kubernetes

Esqueleto de manifiestos en [`deploy/openshift/`](deploy/openshift/) —
Deployment con sondas de liveness (`/health`) y readiness (`/ready`)
separadas, Service, Route y ConfigMap/Secret. **No se probó contra un
cluster real** (no había uno disponible en este entorno); ver el
[README de esa carpeta](deploy/openshift/README.md) para qué cambia
al migrar desde Swarm (solo la capa de orquestación) y qué no (el
código del server).

## Prueba de carga y verificaciones de escalado horizontal (Fase 4, Parte B)

[`deploy/dev/load_test.py`](deploy/dev/load_test.py) es un script simple
(sin `hey` ni `locust`, solo `fastmcp.Client`) que abre N sesiones MCP
concurrentes y mide throughput/latencias:

```bash
uv run python deploy/dev/load_test.py \
  --url http://localhost:8000/mcp \
  --tool resumen_cliente \
  --identificador V16760320 \
  --concurrency 20 \
  --requests-per-worker 20
```

### Lo que se verificó bajo carga real (no teoría) y los números medidos

Todo lo de abajo se corrió contra Postgres y el stub de saldos reales de
`docker-compose.dev.yml`, con réplicas reales del server (procesos/
contenedores independientes, no simulados) generando y sirviendo tráfico
concurrente real.

**1. Fórmula de capacidad (`límite por réplica = techo de la fuente ÷ nº de réplicas`).**
Se levantaron 3 réplicas reales, cada una con `MAX_POOL_SIZE=3` y
`MAX_CONCURRENCY=3` para Postgres, y se les generó carga concurrente
simultánea a las tres (10 sesiones × 5 peticiones por réplica). Se
monitoreó `pg_stat_activity` en Postgres cada segundo durante toda la
corrida: el conteo de conexiones se mantuvo **estable en 9 conexiones de
aplicación** (3 réplicas × pool de 3) durante todo el pico de carga,
nunca más. `/diagnostics` de cada réplica confirmó `pool_size: 3` (el
máximo configurado) en las tres al mismo tiempo. La fórmula se sostiene:
9 = 3 × 3, ni una conexión más.

**2. Presupuesto de conexiones y el umbral de PgBouncer.**
El Postgres de desarrollo (`postgres:16-alpine`, default de fábrica) tiene
`max_connections = 100`. Con `superuser_reserved_connections` (3 por
defecto) reservadas, quedan ~97 conexiones utilizables para la
aplicación. Con la fórmula de arriba, **el umbral para necesitar
PgBouncer (u otro pooler externo) es cuando `nº_de_réplicas × max_pool_size`
se acerca a ese número** — por ejemplo, con `max_pool_size=5` por
réplica, el límite práctico ronda las **19 réplicas** (19 × 5 = 95) antes
de agotar `max_connections`; con `max_pool_size=10`, el límite baja a
**~9 réplicas**. Este número es específico de esta instancia de Postgres
(el `max_connections` real de producción puede ser mayor o menor, y hay
que restarle lo que usen otros clientes — pgAdmin, otros servicios); la
fórmula general es: `nº_réplicas_máximo ≈ (max_connections − reservadas − otros_clientes) ÷ max_pool_size`.

**3. Comportamiento al saturar el semáforo: espera o falla limpio, nunca encola infinito.**
Con `max_concurrency=2` y `acquire_timeout_seconds` generoso (3s), 20
sesiones concurrentes contra una réplica completaron **50/50 peticiones
exitosas** (0 fallos) — todas esperaron su turno y fueron atendidas,
con latencia p95 de ~2.03s (el costo real de encolarse detrás de un
límite de 2). Con el mismo `max_concurrency=2` pero
`acquire_timeout_seconds=0.05` (deliberadamente agresivo), la misma carga
de 20 sesiones concurrentes produjo **13/60 peticiones rechazadas
limpiamente** (`ToolError`: "La base de datos de clientes no está
disponible en este momento") en vez de acumularse indefinidamente —
confirmando las dos rutas posibles del diseño: esperar (si el timeout lo
permite) o fallar limpio (si no), nunca una cola sin fin. El circuito
**permaneció `closed`** durante toda esta prueba: saturación de
concurrencia NO cuenta como fallo de infraestructura para el breaker (es
la distinción documentada en la Fase 2).

**4. `/ready` bajo carga y con una fuente caída: nunca se degrada.**
Con el stub de saldos apagado (`docker stop`) — el conector con
`healthy: false` y el circuito de esa fuente `open` — y simultáneamente
saturando el semáforo de Postgres con 20 sesiones concurrentes contra un
límite de 2, se sondeó `/ready` continuamente: **45/45 respuestas fueron
`200`**, ni una sola `503`. `/ready` siguió respondiendo únicamente sobre
la salud del proceso, exactamente como se diseñó desde la Fase 2 — nunca
se acopla a la salud de los conectores, sin importar cuánta carga o cuántas
fuentes estén caídas.

**5. El circuit breaker con múltiples réplicas: independiente por diseño, confirmado.**
Con dos réplicas reales corriendo, se detuvo el stub de saldos y se le
mandaron 6 peticiones fallidas seguidas SOLO a la réplica A. Resultado en
`/diagnostics` de cada una, al mismo tiempo:

```
Réplica A (recibió las 6 peticiones fallidas): circuit_state = "open"
Réplica B (no recibió tráfico hacia saldo_api): circuit_state = "closed", healthy = false
```

Cada réplica descubre la fuente caída de forma completamente
independiente, con su propio conteo de fallos — exactamente la decisión
de diseño de la Fase 2, ahora confirmada con réplicas reales en vez de en
teoría. En la práctica esto implica que, con N réplicas, una fuente que
se recupera recibe hasta N sondeos independientes en medio-abierto (uno
por réplica que la tenía marcada como caída), no uno solo coordinado.

**6. Graceful shutdown durante una rotación de réplicas — con matices reales, no solo el camino feliz.**
Se corrió el server en un contenedor Docker real (Linux; ver el porqué
más abajo) con carga concurrente sostenida (3 sesiones, cientos de
peticiones) y se ejecutó `docker stop` (SIGTERM real vía el PID 1 del
contenedor) a mitad de la corrida. Resultado, leído directamente de los
logs:

- **470/470 tool-calls que ya habían arrancado se completaron con éxito**
  (`tool_invocation_completed` con `result: success` para cada
  `CallToolRequest` procesado) — ninguna quedó a medias.
- El apagado completo (`Shutting down` → `Application shutdown complete`)
  tomó **~130 ms**, muy por debajo del
  `MCP_CORP_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS` configurado (8s): uvicorn
  no necesitó agotar ese margen porque las peticiones request/response ya
  estaban resueltas.
- **Hallazgo no anticipado:** el stream SSE de larga duración que usa el
  transporte Streamable HTTP de MCP para cada sesión SÍ se corta de forma
  abrupta al recibir SIGTERM (`ASGI callable returned without completing
  response` en los logs de uvicorn), en vez de esperar el timeout de
  gracia completo. El cliente ve un `ReadError` al intentar cerrar
  ordenadamente su sesión. La garantía que sí se sostiene — y es la que
  importa — es que ninguna *tool call* queda inconclusa; lo que se corta
  es la conexión de transporte de la sesión, no una operación de negocio
  en curso. Documentado como hallazgo real, no maquillado: el diseño
  cumple su promesa central (no tools a medias), pero con un matiz de
  transporte que vale la pena conocer si se opera esto en producción.

**¿Por qué en un contenedor Docker (Linux) y no como proceso nativo de Windows?**
Ver "Qué NO se pudo verificar" abajo — Windows no entrega SIGTERM real a
un proceso hijo desde este sandbox; un contenedor Linux sí, y es
exactamente el mismo mecanismo que usarían Swarm/Kubernetes en
producción (ambos corren contenedores Linux y envían SIGTERM real al
apagar una réplica).

### Qué NO se pudo verificar por límites del entorno

- **Un Swarm o cluster OpenShift/Kubernetes real.** Solo Docker Desktop
  (un único daemon, sin múltiples nodos ni Traefik real corriendo). Las
  verificaciones de arriba se hicieron con réplicas reales (procesos y
  contenedores independientes) apuntando a la misma infraestructura
  compartida, que es el mecanismo real detrás de "una réplica" — pero sin
  un balanceador real (Traefik/Service de Kubernetes) al frente
  redirigiendo tráfico entre ellas.
- **Graceful shutdown enviando SIGTERM a un proceso nativo de Windows.**
  Windows no invoca los manejadores de señal que instala uvicorn cuando
  se envía SIGTERM a un proceso hijo desde otro proceso — `os.kill(pid,
  SIGTERM)` y `taskkill` (sin `/F`) terminan el proceso abruptamente sin
  darle oportunidad de drenar. Se confirmó el rechazo explícito de
  Windows (`taskkill` sin `/F`: "Este proceso se puede terminar solo de
  forma forzada"). Por eso la verificación #6 se hizo contra un
  contenedor Docker (Linux real dentro), que si recibe y maneja SIGTERM
  correctamente — el mismo mecanismo que usa un Swarm o Kubernetes reales
  en producción.
- **Manifiestos de OpenShift contra un cluster real.** Sintaxis validada
  (YAML bien formado, coherente con la documentación de la API de
  Kubernetes/OpenShift), pero nunca aplicados con `oc apply` / `kubectl
  apply` contra un cluster.

## Modo producción y fail-closed del HMAC (Fase 4, Parte B)

Pendiente heredado de la Fase 3: antes, si `MCP_CORP_AUDIT_HMAC_SECRET`
venía vacía, el server solo registraba un warning y arrancaba igual — un
HMAC con clave vacía es determinista y públicamente reproducible (el
mismo problema que motivó abandonar el hash plano), así que ese
enmascaramiento no protegía nada.

Ahora, `Settings` (en `config.py`) tiene un validador que revisa la
combinación al construirse: **si `MCP_CORP_ENVIRONMENT=production` y
`MCP_CORP_AUDIT_HMAC_SECRET` está vacía, el proceso no arranca** —
`pydantic.ValidationError` desde el primer momento, antes de abrir
ningún conector. En cualquier otro entorno (`local`, `staging`), una
clave vacía sigue siendo aceptable y solo produce el warning de
`server.py` — apto para desarrollo, no para el entorno cuyos logs sí se
auditan de verdad.

## Configuración

Toda la configuración viene de variables de entorno (prefijo `MCP_CORP_`),
cargadas con `pydantic-settings` en [`src/mcp_corp/config.py`](src/mcp_corp/config.py).
Ver [`.env.example`](.env.example) para la lista completa con valores de
ejemplo. **Nunca** commitees un `.env` real: está excluido en `.gitignore`.

### Secretos como archivo (Docker Swarm / Kubernetes, Fase 5)

Docker Swarm monta cada secreto como un ARCHIVO en `/run/secrets/<nombre>`,
no como variable de entorno — y pydantic-settings solo sabe leer variables
de entorno. `config.py` resuelve esto con el patrón `<VAR>_FILE` (el mismo
que usan las imágenes oficiales de Postgres/MySQL/Redis en Docker Hub):

```bash
# En vez de exportar el valor directamente...
export MCP_CORP_AUDIT_HMAC_SECRET_FILE=/run/secrets/audit_hmac_secret
export MCP_CORP_POSTGRES__DSN_FILE=/run/secrets/postgres_dsn
```

Al arrancar, `get_settings()` lee cada archivo referenciado por una
variable `MCP_CORP_*_FILE` y su contenido (recortado) pasa a ocupar la
variable sin el sufijo — `MCP_CORP_AUDIT_HMAC_SECRET_FILE=/run/secrets/x`
termina resolviendo `MCP_CORP_AUDIT_HMAC_SECRET` como si se hubiera
exportado directamente. Funciona igual para CUALQUIER variable con
prefijo `MCP_CORP_`, incluidas las anidadas (`MCP_CORP_POSTGRES__DSN_FILE`)
— no hace falta enumerar cuáles son sensibles ni tocar el código cuando se
agregue una nueva.

**Precedencia** (de mayor a menor): variable de entorno real explícita >
archivo referenciado por `_FILE` > `.env` de desarrollo > default del
campo. Si `MCP_CORP_X` ya existe como variable real, la variante `_FILE`
se ignora en silencio — por eso el flujo de desarrollo local con `.env`
sigue funcionando exactamente igual que antes de esta fase, sin ningún
cambio.

**¿Por qué este patrón y no `secrets_dir` nativo de pydantic-settings?**
Ver el docstring de `_load_file_secrets_into_environ` en `config.py` para
el detalle: `secrets_dir` resuelve nombres de archivo a partir del nombre
de cada campo, y su comportamiento con modelos anidados (`postgres.dsn`,
`saldo_api.*`) no está bien cubierto en la documentación. El patrón
`_FILE` opera sobre el nombre de la variable de entorno ya resuelta por
pydantic-settings (prefijo + delimitador `__` incluidos), así que cubre
cualquier campo, anidado o no, sin depender de ese comportamiento interno.

## Estructura del proyecto

```
src/mcp_corp/
├── main.py                    # entrypoint: python -m mcp_corp
├── server.py                  # FastMCP server: /health, /ready, /diagnostics, tools/resource/prompt, lifecycle
├── config.py                  # configuración vía pydantic-settings (Postgres + saldo_api)
├── logging_setup.py           # logging JSON estructurado a stdout
├── audit.py                   # auditoría por invocación de tool (correlation id + enmascaramiento)
├── tools.py                   # las 3 tools de negocio + Resource + Prompt (Fase 3)
├── identifiers.py             # normalización/validación de cédula-RIF venezolano (Fase 4)
└── connectors/
    ├── base.py                # protocolo Connector: connect/close/health/run
    ├── resilience.py          # capa genérica: semáforo + timeout + circuit breaker
    ├── registry.py            # ciclo de vida y diagnóstico agregado de conectores
    ├── postgres.py            # conector concreto: psycopg3 + psycopg_pool
    └── http.py                 # conector concreto: httpx.AsyncClient (Fase 3)

tests/
├── test_audit.py                       # unitarios de auditoría: enmascaramiento, forma del log
├── test_identifiers.py                 # unitarios de normalización/checksum (Fase 4)
├── test_config.py                      # fail-closed del HMAC en modo producción (Fase 4)
├── connectors/
│   ├── test_resilience.py              # unitarios de la capa de resiliencia (conector falso)
│   ├── test_postgres_integration.py    # integración contra Postgres real
│   └── test_http.py                    # unitarios del conector HTTP (transporte falso)
└── tools/
    ├── test_tools_logic.py             # unitarios de las tools: parcial, fuente caída, sin internals
    └── test_tools_integration.py       # integración contra Postgres + stub de saldos reales

deploy/
├── swarm/                  # docker-compose.yml para Docker Swarm/Portainer
├── dev/                     # postgres-seed.sql + saldo_api_stub.py + load_test.py
└── openshift/               # Deployment/Service/Route/ConfigMap + Secret de ejemplo (Fase 4)
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

### Fase 4, Parte A — identificadores venezolanos

**¿Por qué normalizar en el servidor y no exigirle el formato limpio al modelo?**
Un usuario le escribe al agente "consúltame la cédula V-16.760.320", no
"16760320". Si la tool exige dígitos limpios, el modelo tiene que adivinar
cómo limpiar la entrada — y si adivina distinto de cómo lo hace nuestro
código, la tool falla antes de tocar ninguna fuente por una razón que no
tiene nada que ver con si el cliente existe. La normalización es
responsabilidad del servidor, no del modelo: `identifiers.py` acepta el
formato tal como lo escribe una persona.

**¿Por qué un módulo dedicado (`identifiers.py`) y no dentro de `tools.py`?**
Es lógica de dominio (reglas del SENIAT) sin ninguna dependencia de
FastMCP, de un conector ni de resiliencia — se puede probar y razonar
sobre ella de forma completamente aislada. `tools.py` la importa y la usa,
pero no la conoce por dentro.

**El algoritmo del dígito verificador: cómo se verificó.**
Se cruzaron tres implementaciones independientes y no relacionadas entre
sí — un gist de la lista python-venezuela, el paquete `joseayram/utils`
en PHP, y la librería `django-localflavor-ve` (usada en producción por
proyectos Django venezolanos) — y las tres coinciden EXACTAMENTE en la
fórmula: peso por posición `(3, 2, 7, 6, 5, 4, 3, 2)` sobre los 8 dígitos
del número, más un valor base por letra (`V=4, E=8, J=12, P=16, G=20`),
todo módulo 11. Como confirmación final, se encontró un ejemplo real
citado como correcto en una fuente independiente
(`V-13222105-3`, documento "Cálculo Dígito verificador RIF Venezuela",
marcado "Rif correcto") y se reprodujo el cálculo exacto con esta
implementación: suma = 63, residuo = 8, verificador = 11 − 8 = 3. Con esa
triple coincidencia de código más un ejemplo real verificado, **se activó
el checksum por defecto** (`validar_digito_verificador=True`) — no quedó
detrás de un flag apagado, porque sí se logró la confianza que pedía el
encargo. Sigue siendo desactivable (`MCP_CORP_IDENTIFIERS__VALIDAR_DIGITO_VERIFICADOR=false`)
por si en producción aparece un caso real que la fórmula no contemple.

**La letra `I` no es un prefijo válido — trampa conocida, verificada y evitada.**
Ninguna de las fuentes oficiales ni las tres implementaciones cruzadas
incluye `I` como prefijo. Sí aparece en algunos regex y librerías de
validación de terceros, heredado de un error que se propaga por copia
entre proyectos. `identifiers.py` solo acepta `V, E, J, G, P` por defecto,
y hay un test de regresión explícito (`test_letra_i_es_rechazada_no_existe_en_el_seniat`)
para que nadie la reintroduzca sin darse cuenta.

**El prefijo `C`: activable, no cableado.**
Existe desde un anuncio oficial de 2015 para comunas, consejos comunales
y organizaciones del Poder Popular. Pero a diferencia de `V/E/J/G/P`, no
se encontró consenso entre las fuentes consultadas sobre si sigue vigente
en el set que valida el portal actual del SENIAT, ni una fórmula de
dígito verificador confirmada por más de una fuente para esta letra (solo
`joseayram/utils` la documenta, compartiendo el valor de `J`). Por esa
doble incertidumbre queda deshabilitada por defecto
(`MCP_CORP_IDENTIFIERS__INCLUIR_PREFIJO_C=false`) y solo se activa
explícitamente.

**Relación cédula/RIF: no son dos números independientes.**
Para personas naturales (prefijo `V`), los 8 dígitos del RIF SON el
número de cédula — no hay dos identificadores distintos que reconciliar.
Por eso `IdentidadFiscal` guarda un solo `numero` de 8 dígitos y expone
`.cedula` (forma corta, sin verificador) y `.rif` (forma completa, exige
verificador) como dos VISTAS del mismo dato, no como dos campos separados.

**Relleno con cero.**
Un identificador con menos de 8 dígitos (cédulas antiguas más cortas) se
completa con ceros a la izquierda hasta 8 — `identidad.numero.zfill(8)`.
`"123456"` normaliza a `"00123456"`.

**Cada conector adapta la forma canónica a lo que necesita su fuente.**
`tools.py` normaliza una sola vez (`identidad = normalizar(...)`) y pasa
`identidad.cedula` a Postgres y a la API de saldos — en esta fase ambas
fuentes usan la misma forma corta, pero el punto de extensión ya existe:
un conector futuro que necesite el RIF completo con verificador usaría
`identidad.rif` en su lugar, sin que el resto del código cambie.

**Rechazo sin tocar ninguna fuente: la razón de ser en el diseño de resiliencia.**
`_resolve_identidad()` corre ANTES de cualquier `ResilientExecutor.run()`.
Un identificador mal tipeado nunca reserva un slot del semáforo ni abre
una conexión del pool — es el filtro más barato posible, y en un sistema
donde cada fuente tiene un techo de concurrencia finito, filtrar temprano
importa.

## Próximas fases (fuera de alcance aquí)

- Fase 5+: gateway de gobierno, más tools de negocio, más conectores
  (sistemas legacy) sobre el mismo protocolo `Connector` / `ResilientExecutor`.
- Token bucket para límite de tasa (req/s) por fuente — hoy solo hay
  límite de concurrencia (ver `rate_limit_per_second` en `config.py`).
- Estado del circuit breaker compartido entre réplicas (hoy es por
  réplica, a propósito — ver "Decisiones de diseño").
- Verificar los manifiestos de OpenShift/Kubernetes contra un cluster real
  (`deploy/openshift/`) — la sintaxis y la estructura están listas, pero
  nunca se aplicaron con `oc`/`kubectl`.
- PgBouncer (u otro pooler externo) delante de Postgres, cuando el número
  de réplicas se acerque al umbral documentado en "Verificaciones bajo
  carga".
- Continuidad de correlación del HMAC de auditoría durante una rotación
  de clave planificada (hoy rotar rompe la correlación histórica a
  propósito; ver Fase 3).
