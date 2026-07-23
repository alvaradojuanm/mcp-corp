Continuamos el proyecto mcp-corp. La FASE 1 (andamiaje base) está CERRADA y mergeada en `main`:
server FastMCP 3.4.4 sobre Streamable HTTP, stateless, con config vía pydantic-settings,
logging JSON con correlation id, /health y /ready separados, graceful shutdown y contenedor
no-root verificado. Partimos de ahí.

Esta es la FASE 2: la CAPA DE CONECTORES. NO implementes tools MCP todavía — eso es la Fase 3.
Esta fase construye la infraestructura sobre la que las tools se apoyarán después.

## El norte de esta fase
Cada fuente de datos (bases de datos, APIs REST, sistemas legacy) debe quedar encapsulada en un
módulo autocontenido que sepa DOS cosas: cómo hablarle a su fuente, y cómo protegerse a sí mismo
y a la fuente. Cuando en fases futuras sumemos 50 fuentes, agregar una debe ser "escribir cómo
hablo con ella", no reimplementar resiliencia.

## DECISIÓN DE ARQUITECTURA CENTRAL — dos capas, no una
NO hagas un "conector Postgres" que adentro tenga su semáforo, su timeout y su breaker, y luego
repitas esa lógica en cada conector nuevo. Sepáralo:

1. **Capa genérica de resiliencia** — no sabe nada de Postgres ni de HTTP. Solo sabe limitar
   concurrencia, cortar por timeout y abrir/cerrar el circuito. Se escribe UNA vez.
2. **Conectores concretos** — cada uno solo sabe cómo hablarle a su fuente. La resiliencia la
   reciben envueltos por la capa de arriba.

Define un protocolo/ABC común que todo conector debe cumplir (p. ej. `connect`, `close`,
`health`, y la ejecución de una operación), de modo que la capa de resiliencia envuelva a
cualquiera de forma uniforme.

## ALCANCE EXACTO DE ESTA FASE
Capa genérica de resiliencia **+ UN conector real de PostgreSQL**. El conector real no es opcional:
una abstracción diseñada sin un usuario concreto sale mal, y necesitamos validar la capa contra
algo que de verdad abre conexiones. No implementes conectores de API REST todavía — pero deja el
diseño listo para que agregarlo sea trivial.

## DECISIONES YA TOMADAS (no las reabras, ya fueron investigadas)

**Driver: psycopg3 con su pool async (`psycopg_pool`). NO uses asyncpg.**
La razón es compatibilidad con PgBouncer: cuando escalemos réplicas, PgBouncer en modo
"transaction" no soporta prepared statements, y asyncpg los usa automáticamente, lo que rompe con
`DuplicatePreparedStatementError`. Peor aún, ese fallo SOLO aparece bajo presión del pool —
pasaría todas las pruebas y explotaría en producción. psycopg3 maneja esto mejor por diseño con su
`prepare_threshold` adaptativo. La diferencia de rendimiento entre ambos drivers no es nuestro
cuello de botella.

**Circuit breaker: implementación propia, NO una librería externa.**
Las semánticas son simples (cerrado / abierto / medio-abierto, con umbral de fallos y tiempo de
reset) y son ~80 líneas. Las alternativas de PyPI son proyectos de un solo mantenedor, y este
código va en el camino crítico de un sistema bancario: cada dependencia que no metemos es
superficie de supply chain que no tenemos que defender. Código propio, tipado y auditable.

**Concurrencia: `asyncio.BoundedSemaphore`, UNO POR FUENTE (no global).**
Bounded (no el `Semaphore` normal) porque atrapa bugs de over-release. Por fuente, para que el
backpressure de cada una sea independiente: si el core bancario se satura, eso no debe afectar
las consultas a Postgres.

**Timeouts: `asyncio.timeout()` (Python 3.11+), NO `asyncio.wait_for`.**
Y si en algún punto necesitas concurrencia estructurada, `asyncio.TaskGroup`, NO `asyncio.gather`
(gather deja tareas huérfanas corriendo cuando una falla).

**`/ready` NO se acopla a la salud de las fuentes.**
Esto contradice una promesa del README de la Fase 1 — corrígela ahí. El razonamiento: si el
breaker de una fuente se abre y eso pone `/ready` en 503, el balanceador saca la réplica entera de
rotación y las demás tools que SÍ funcionan dejan de atenderse. Empeora la situación. `/ready`
sigue reflejando solo el estado del proceso. El estado de los conectores se expone por separado
(ver abajo).

## REQUISITOS DE CADA PIEZA

### Capa de resiliencia (`src/mcp_corp/connectors/resilience.py` o similar)
- **Límite de concurrencia** por fuente con `BoundedSemaphore`. Si el límite está saturado, la
  petición espera; si espera más de lo razonable, falla limpio (no encolar infinito).
- **Timeout** por operación con `asyncio.timeout()`.
- **Circuit breaker** propio con los tres estados. Configurables: umbral de fallos consecutivos
  para abrir, tiempo antes de pasar a medio-abierto, y cuántos éxitos se requieren para cerrar.
  Distingue fallos de infraestructura (que cuentan para abrir el circuito) de errores de negocio
  legítimos (que NO deben abrirlo).
- **Estado del breaker es POR RÉPLICA.** Cada réplica descubre una fuente caída de forma
  independiente. Esto es intencional; documéntalo como decisión, no lo dejes como accidente.
  (Estado compartido vía Redis queda como opción futura, no ahora.)
- Cada evento relevante (circuito abre, circuito cierra, timeout, límite saturado) debe quedar en
  el **log estructurado JSON** con el correlation id que ya existe.
- Errores hacia afuera: nunca filtres internals (ni stack traces, ni strings de conexión, ni SQL).

### Conector PostgreSQL (`src/mcp_corp/connectors/postgres.py` o similar)
- psycopg3 + `psycopg_pool` async, con pool acotado y configurable.
- Ciclo de vida atado al lifespan del server: el pool se abre al arrancar y se cierra limpio en el
  graceful shutdown que ya funciona.
- Un método de health propio (algo tipo `SELECT 1`) que la capa de diagnóstico pueda consultar.
- Credenciales SOLO desde config/entorno. Cero hardcode, cero valores reales en el repo.
- Consultas siempre parametrizadas — nunca concatenación de strings en SQL.

### Configuración por fuente
Cada conector con su propio bloque de config: `max_concurrency`, `timeout`, umbral y reset del
breaker, tamaño de pool, credenciales. Refleja todo en `.env.example`.

Incluye también un campo preparado (aunque no se use aún) para **límite de tasa (req/s)**, con un
comentario explicando que el semáforo limita concurrencia, NO tasa — si una fuente declara un
techo en req/s hará falta un token bucket encima. Es un hueco conocido, déjalo señalado.

### Endpoint de diagnóstico
Expón el estado de cada conector (estado del breaker, uso del pool, última verificación de salud)
en un endpoint SEPARADO de `/health` y `/ready` — algo como `/diagnostics` o `/connectors`. Sirve
para observar y alertar sin sacar la réplica del balanceo.

### Tests
- **Unitarios de la capa de resiliencia** con un conector falso: es fácil forzar fallos y verificar
  que el breaker abre al umbral, que pasa a medio-abierto tras el reset, que cierra al recuperarse,
  que el timeout dispara y que el semáforo limita de verdad. Esta capa DEBE quedar bien cubierta.
- **Integración del conector Postgres** contra un Postgres real levantado con docker-compose de
  desarrollo (añádelo en `deploy/` o `docker-compose.dev.yml`, con datos semilla mínimos).

### Documentación (README)
- Explica la separación en dos capas y por qué.
- Documenta la fórmula de capacidad: **límite por réplica = techo de la fuente ÷ nº de réplicas**.
  Con un ejemplo numérico. Es la regla que evita que al escalar réplicas ahoguemos la fuente.
- Documenta las decisiones de esta fase con el mismo nivel de la sección "Decisiones de diseño"
  que ya existe: por qué psycopg3, por qué breaker propio, por qué estado por réplica, por qué
  `/ready` no se acopla a las fuentes.
- Corrige la promesa del README de la Fase 1 sobre `/ready` reflejando la salud de conectores.

## ANTES DE ESCRIBIR CÓDIGO
Verifica en PyPI/documentación oficial las versiones actuales y estables de `psycopg[binary]` y
`psycopg_pool`, y confirma la API vigente del pool async (`AsyncConnectionPool`: apertura,
cierre, y el patrón recomendado de ciclo de vida — hubo cambios en cómo se abre el pool en
versiones recientes). PINEA versiones exactas. Si algo no lo puedes verificar, dímelo en vez de
inventar.

## LECCIONES DE LA FASE 1 — aplícalas
1. **"El build pasa" ≠ "funciona".** En la Fase 1 un bug pasó el build limpio y reventó al
   arrancar el contenedor. Verifica que las cosas CORRAN, no solo que compilen.
2. Si NO puedes verificar algo por límites de tu entorno (sin daemon de Docker, sin Postgres),
   **dilo explícitamente** en tu reporte en vez de asumir que funciona. Yo lo valido en mi máquina.
3. Al terminar, corre la auditoría de dependencias:
   `uv export --no-hashes --format requirements-txt > requirements-audit.txt`
   `uvx pip-audit -r requirements-audit.txt`
   Y añade `requirements-audit.txt` al `.gitignore` (es un artefacto derivado).

## GIT
- Identidad ya configurada: `alvaradojuanm` / `114210637+alvaradojuanm@users.noreply.github.com`.
  VERIFÍCALA antes de commitear y corrígela si el entorno la reseteó.
- **POLÍTICA DE AUTORÍA OBLIGATORIA:** todos los commits ÚNICAMENTE bajo `alvaradojuanm`. NO
  añadas `Co-authored-by:`. NO añadas firmas de herramienta ("Generated with...", 🤖, ni nada
  similar). NO te acredites de ninguna forma. El mensaje contiene solo la descripción del cambio.
- Trabajamos trunk-based: parte de `main` actualizado. Commits atómicos y descriptivos.
- Guarda una copia EXACTA de este prompt como `docs/prompts/fase-02.md`.
- NUNCA `.env`, credenciales ni secretos en ningún commit.
- Verifica con `git log --format='%an <%ae>'` antes de empujar.

## AL TERMINAR, REPORTA
1. Versiones exactas pineadas de psycopg y psycopg_pool.
2. Qué archivos creaste y la estructura resultante de `connectors/`.
3. Resultado de los tests (unitarios de resiliencia + integración con Postgres).
4. Resultado de `pip-audit`.
5. Qué NO pudiste verificar por límites del entorno.
6. Cualquier decisión donde tuviste que elegir por mí.
7. Confirmación de que la autoría quedó limpia.

NO avances a tools MCP. Esta fase termina en la capa de conectores con Postgres funcionando.

Si algo es ambiguo o no puedes verificar contra la documentación actual, PREGUNTA antes de asumir.
Prefiero corregir el rumbo ahora que rehacer después.
