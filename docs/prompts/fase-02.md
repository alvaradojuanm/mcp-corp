# Fase 02 — Capa de conectores

| Campo | Valor |
|---|---|
| **Fase** | 02 |
| **Objetivo** | Capa genérica de resiliencia + primer conector real (PostgreSQL). Sin tools MCP. |
| **Repo** | `github.com/alvaradojuanm/mcp-corp` |
| **Commits** | `568e23b` → `5a0ee55` (5 commits, pusheados a `main`) |
| **Estado** | ✅ **CERRADA** |
| **Fecha de cierre** | 23 de julio de 2026 |
| **Versiones pineadas** | `psycopg[binary]==3.3.4` · `psycopg-pool==3.3.1` |

---

## Resultado

Capa de resiliencia genérica y conector PostgreSQL funcionando, validados de punta a punta contra un Postgres real. **A diferencia de la Fase 1, el agente sí tuvo Docker disponible y no quedaron puntos ciegos de verificación.**

### Estructura resultante

```
src/mcp_corp/connectors/
├── base.py         # Protocol Connector: connect / close / health / run
├── resilience.py   # BoundedSemaphore por fuente + asyncio.timeout() + CircuitBreaker propio
├── registry.py     # ciclo de vida de conectores + agregación de /diagnostics
└── postgres.py     # psycopg_pool.AsyncConnectionPool

tests/
├── conftest.py
└── connectors/
    ├── test_resilience.py            # unitarios con FakeConnector
    └── test_postgres_integration.py  # integración contra Postgres real

docker-compose.dev.yml
deploy/dev/postgres-seed.sql
```

**Modificados:** `config.py` (bloque `PostgresSettings`), `server.py` (`/diagnostics`, `/ready` corregido), `main.py` (wiring del registry), `.env.example`, `README.md`, `.gitignore`, `pyproject.toml`, `uv.lock`.

---

## Verificaciones de aceptación

- [x] **12/12 tests pasando** contra Postgres real (no mocks)
- [x] 7 unitarios de resiliencia: semáforo limitando concurrencia real, timeout de espera de slot, timeout de operación, apertura del breaker al umbral, medio-abierto tras reset (con reloj falso), reapertura ante fallo en medio-abierto, y que **errores de negocio nunca abren el circuito**
- [x] 5 de integración: conexión + health real, query parametrizada, `pool_stats()`, ejecución envuelta en `ResilientExecutor`, y rechazo tras `close()`
- [x] Server end-to-end con `MCP_CORP_POSTGRES__ENABLED=true`
- [x] `/health` y `/ready` intactos — **`/ready` NO se acopla a la salud de las fuentes**
- [x] `/diagnostics` reportando `circuit_state`, `in_flight` y stats reales del pool
- [x] Campo de límite de tasa reservado y documentado en 4 lugares (`.env.example`, `config.py`, `resilience.py`, `README.md`)
- [x] `pip-audit`: 82 paquetes, **`No known vulnerabilities found`**
- [x] `requirements-audit.txt` excluido en `.gitignore`
- [x] README de la Fase 1 corregido respecto a `/ready`
- [x] Autoría verificada: solo `alvaradojuanm` + noreply, sin coautorías

---

## Decisiones tomadas por el agente

| Decisión | Veredicto |
|---|---|
| `psycopg[binary]==3.3.4` + `psycopg-pool==3.3.1` | ✅ Conforme a lo investigado (psycopg3 por compatibilidad con PgBouncer) |
| Patrón `AsyncConnectionPool(open=False)` + `await pool.open(wait=True)` explícito | ✅ Verificado contra docs — abrir en el constructor está deprecado |
| `registry.py` (no estaba en el prompt) | ✅ **Buena adición.** Centraliza ciclo de vida y agregación de diagnostics; evita wiring regado cuando lleguen 50 fuentes |
| Fix de `WindowsSelectorEventLoopPolicy` | ✅ Aprobada con nota (ver abajo) |
| Errores de negocio cuentan como **éxito** para el breaker | ✅ Aprobada — si la fuente respondió, la fuente está sana |
| Medio-abierto reabre al **primer** fallo | ✅ Aprobada — semántica estándar |

### Hallazgo: psycopg async no corre sobre ProactorEventLoop (Windows)

No estaba anticipado en el prompt. El default de asyncio en Windows es `ProactorEventLoop`, incompatible con psycopg async.

- **Fix:** `WindowsSelectorEventLoopPolicy` en `tests/conftest.py` y, condicionado a `sys.platform == "win32"`, en `main.py`.
- **Impacto:** ninguno en el contenedor Linux de producción.
- **Nota abierta:** queda código específico de plataforma en `main.py`, no solo en tests. Está condicionado y documentado, y permite desarrollar en local sin fricción — pero es código de conveniencia de desarrollo viviendo en el entrypoint de producción. Fichado, no bloqueante.

### Nota sobre "errores de negocio cuentan como éxito"

Coherente, pero con una consecuencia a tener presente: **en estado medio-abierto, una respuesta con error de negocio ayuda a cerrar el circuito.** Es correcto bajo el criterio elegido (la fuente respondió), pero conviene saberlo si alguna vez el comportamiento del breaker sorprende.

---

## Diseño consolidado en esta fase

**Separación en dos capas.** La capa de resiliencia no sabe nada de Postgres: solo limita concurrencia, corta por timeout y abre/cierra el circuito. Los conectores concretos solo saben hablarle a su fuente. Agregar una fuente nueva = escribir cómo se le habla; la resiliencia se hereda.

**PostgreSQL como primer inquilino, no como caso único.** Se eligió por ser el más exigente (pool, ciclo de vida, credenciales) y por estar disponible para pruebas reales. Una abstracción sin usuario concreto sale con huecos.

**Estado del breaker por réplica.** Cada réplica descubre una fuente caída de forma independiente. Intencional y documentado. Estado compartido vía Redis queda como opción futura.

**`/ready` desacoplado de las fuentes.** Si el breaker de una fuente abriera `/ready`, el balanceador sacaría la réplica entera de rotación y las tools sanas dejarían de atenderse. El estado de conectores vive en `/diagnostics`, para observar y alertar sin afectar el balanceo.

**Hueco conocido: límite de tasa.** El semáforo limita **concurrencia** (operaciones simultáneas), no **tasa** (req/s). Si una fuente declara un techo en req/s hará falta un token bucket encima del semáforo. Campo `rate_limit_per_second` reservado y documentado como punto de extensión.

**Fórmula de capacidad:** `límite por réplica = techo de la fuente ÷ nº de réplicas`.

---

## Lecciones

1. **El agente puede omitir cosas del resumen sin haberlas omitido del trabajo.** El campo de rate limit estaba implementado en 4 lugares pero no apareció en el reporte. Verificar contra el requisito, no contra el resumen.
2. **Cuando el agente tiene el entorno completo, la calidad sube notablemente.** Con Docker disponible probó todo end-to-end y no quedaron pendientes de validación — contraste directo con la Fase 1.
3. **Tests con reloj falso** para probar transiciones temporales (medio-abierto) en vez de `sleep`: rápidos y deterministas.

---

## Pendientes menores

- [ ] Probar contra la instancia de Postgres 18 existente (`localhost:5433`), distinta a la del compose de desarrollo — verificar que funciona fuera de la burbuja del agente.
- [ ] Evaluar si el fix de Windows debe moverse fuera de `main.py`.

---

## Siguiente

**Fase 03 — Tools:** las primeras tools MCP por intención de negocio sobre los conectores, más las primitivas Resources y Prompts. Es donde el agente MCP por fin ve algo.