Continuamos el proyecto mcp-corp. Las FASES 1 a 5 están CERRADAS y mergeadas en `main`. El
servidor está DESPLEGADO en pre-producción (Docker Swarm + Traefik + HTTPS, dos réplicas,
Postgres real, secretos por archivo, auditoría HMAC con fail-closed).

Esta es la FASE 6: **correcciones de campo**. Son tres fallos reales encontrados al probar el
despliegue de punta a punta — ninguno apareció en los tests de las fases anteriores. NO
implementes features nuevas. Esta fase corrige, prueba y documenta.

═══════════════════════════════════════════════════════════════════════
CONTEXTO: por qué estos bugs sobrevivieron 5 fases
═══════════════════════════════════════════════════════════════════════

Los tests actuales (99/99 pasando) prueban la lógica de cada tool de forma **aislada**, pero
NUNCA levantan el servidor y le piden `tools/list` por el protocolo MCP real. Por eso un
servidor que expone CERO tools pasó toda la suite en verde.

**Cada corrección de esta fase debe venir con un test que reproduzca el fallo ANTES del
arreglo.** Escribe primero el test que falla, luego corrige. Si el test no falla contra el
código actual, no está reproduciendo el bug.

═══════════════════════════════════════════════════════════════════════
BUG 1 — Registro de tools todo-o-nada (CRÍTICO)
═══════════════════════════════════════════════════════════════════════

**Evidencia real, contra el despliegue:**

```
# Postgres sano, circuito cerrado, pool operativo:
GET /diagnostics →
{"connectors":{"postgres":{"circuit_state":"closed","healthy":true,
 "pool":{"pool_size":1,"pool_max":10}}}}

# Pero el catálogo MCP está vacío:
POST /mcp  {"method":"tools/list"} →
{"jsonrpc":"2.0","id":2,"result":{"tools":[]}}

# Y en los logs, en cada arranque:
{"message":"tools_not_registered_missing_connectors",
 "postgres_registrado":true,"saldo_api_registrado":false}
```

**El problema:** con `MCP_CORP_SALDO_API__ENABLED=false` y Postgres perfectamente sano, el
servidor no registra NINGUNA tool — ni siquiera `consultar_cliente`, que solo depende de
Postgres. El registro es todo-o-nada.

**La corrección:** **cada tool se registra si y solo si SUS PROPIAS fuentes están disponibles.**

- `consultar_cliente` → requiere solo Postgres → se registra si Postgres está habilitado
- `consultar_saldo` → requiere solo la API de saldos → se registra si esa API está habilitada
- `resumen_cliente` → requiere AMBAS → se registra solo si ambas están habilitadas

Y el Resource y el Prompt: evalúa si dependen de alguna fuente y aplica el mismo criterio.

El log de arranque debe decir con claridad **qué tools quedaron registradas y cuáles no, con el
motivo de cada exclusión** — hoy dice "no se registraron tools" sin decir cuáles ni por qué,
que es justo lo que hizo que el fallo pasara desapercibido.

**Tests requeridos:** matriz completa de combinaciones (solo Postgres / solo API / ambas /
ninguna), verificando en cada caso exactamente qué tools quedan expuestas. Y un test que
levante el servidor y consulte `tools/list` **por el protocolo MCP real**, no inspeccionando
variables internas. Ese test es el que debió existir desde la Fase 3.

═══════════════════════════════════════════════════════════════════════
BUG 2 — El servidor muere si una fuente está caída al arrancar
═══════════════════════════════════════════════════════════════════════

**Evidencia real** (ocurrió con credenciales incorrectas de Postgres):

```
File "mcp_corp/server.py", line 71, in lifespan
    await connector_registry.connect_all()
File "mcp_corp/connectors/postgres.py", line 67, in connect
    await self._pool.open(wait=True, timeout=...)
psycopg_pool.PoolTimeout: pool initialization incomplete after 10.0 sec
```

El proceso entero muere y las réplicas entran en bucle de reinicio.

**Por qué es grave:** contradice la tesis de la capa de resiliencia. El circuit breaker existe
para que una fuente caída no tumbe todo el servicio — pero solo protege DURANTE las
operaciones, no en el arranque. En producción, un reinicio de Postgres en una ventana de
mantenimiento dejaría todas las réplicas caídas, incluyendo las tools que no dependen de él.

**La corrección: arranque en modo degradado.** Si un conector no logra conectar al arrancar:

- El servidor **arranca igual**.
- Ese conector queda con el **circuito abierto** y lo refleja en `/diagnostics`.
- Las tools que dependen SOLO de fuentes sanas quedan disponibles (se combina con el Bug 1).
- Las tools que dependen de la fuente caída responden con el error de negocio limpio que ya
  existe ("información no disponible temporalmente"), sin filtrar internals.
- **El conector debe reintentar en segundo plano** y, cuando la fuente vuelva, cerrar el
  circuito y quedar operativo — sin requerir un reinicio manual del servicio.
- El fallo queda en el log estructurado con nivel apropiado.

**Decisión de diseño que debes tomar y justificar:** cuando un conector se recupera en
caliente, ¿se registran dinámicamente las tools que dependían de él? MCP soporta notificar
cambios en la lista de tools (`notifications/tools/list_changed`). Verifica qué soporta FastMCP
3.x al respecto. Si el registro dinámico resulta complejo o frágil, es aceptable registrar las
tools desde el arranque y que fallen limpio mientras la fuente esté caída — pero **documenta
cuál elegiste y por qué**.

**Tests requeridos:** el servidor arranca con una fuente inalcanzable; `/diagnostics` refleja el
circuito abierto; las tools de fuentes sanas funcionan; las de la fuente caída dan error limpio;
y la recuperación en caliente funciona sin reiniciar.

═══════════════════════════════════════════════════════════════════════
BUG 3 — Las sesiones MCP se rompen con múltiples réplicas (INVESTIGAR, NO IMPLEMENTAR)
═══════════════════════════════════════════════════════════════════════

**Evidencia real, con 2 réplicas detrás de Traefik:**

```
POST /mcp {"method":"initialize"} → 200, mcp-session-id: a242c38a...
POST /mcp {"method":"tools/list"} con ese session-id →
{"error":{"code":-32600,"message":"Session not found"}}
```

Con **1 réplica** la misma secuencia funciona. Confirmado: el `initialize` crea la sesión en
memoria de una réplica y la siguiente petición cae en otra que no la conoce.

**Esto contradice la premisa de "stateless" que arrastramos desde la Fase 1.** Es cierta para
las tools (request/response), pero el `StreamableHTTP session manager` de MCP SÍ mantiene
estado de sesión en memoria.

**En esta fase NO lo implementes. Investiga y documenta las opciones:**

1. **Estado de sesión compartido (Redis).** Es lo que hizo IBM ContextForge para funcionar en
   entornos distribuidos. Verifica qué ofrece FastMCP 3.x de forma nativa.
2. **Sticky sessions en Traefik.** Afinidad por cookie o por el header `Mcp-Session-Id`.
   Verifica si Traefik v2.11 puede hacer afinidad por header — es más simple pero degrada el
   balanceo.
3. **Delegar en el gateway.** Vamos a poner un gateway de gobierno delante (fase siguiente). Si
   ese gateway mantiene la sesión del lado del cliente y habla con el servidor por su cuenta, el
   problema podría desaparecer sin tocar nuestro código. **Esta opción es la que puede
   ahorrarnos más trabajo — evalúala con cuidado.**

**Entregable:** una sección en el README con las tres opciones, sus implicaciones y tu
recomendación. **Ninguna implementación en esta fase.** La decisión la tomamos con la
información en mano.

Anota también el hallazgo relacionado de la Fase 4: el stream SSE se corta ante SIGTERM, así
que en cada rolling update los clientes pierden la sesión y deben reconectar. Es el mismo
problema visto desde otro ángulo.

═══════════════════════════════════════════════════════════════════════
ENTREGA
═══════════════════════════════════════════════════════════════════════

- **Nueva versión de imagen:** el despliegue actual corre `mcp-corp:1.0.1`. Sube la versión en
  `pyproject.toml` (hoy dice `0.1.0`, desalineado con el tag de la imagen — alinéalos) y
  documenta el tag a construir.
- **Crea un tag de Git** con la versión, para que se pueda saber exactamente qué commit
  corresponde a la imagen desplegada.
- Actualiza el README con: el criterio de registro de tools, el comportamiento en modo
  degradado, y la sección de sesiones con las tres opciones.
- Al terminar corre la auditoría:
  `uv export --no-hashes --format requirements-txt > requirements-audit.txt`
  `uvx pip-audit -r requirements-audit.txt`

## LECCIONES DE LAS FASES ANTERIORES — aplícalas
1. **Escribe primero el test que falla.** Si no falla contra el código actual, no reproduce el bug.
2. **"El build pasa" ≠ "funciona".** Estos tres bugs pasaron 99 tests en verde.
3. **Lo que se cablea pero no se usa, no se valida.** El registro de tools nunca se ejercitó por
   el protocolo real hasta que se probó en despliegue.
4. Si NO puedes verificar algo por límites del entorno, **dilo explícitamente**.
5. **Reporta completo:** si el prompt lo pide, que aparezca en el reporte.

## GIT
- Identidad: `alvaradojuanm` / `114210637+alvaradojuanm@users.noreply.github.com`. VERIFÍCALA.
- **POLÍTICA DE AUTORÍA OBLIGATORIA:** commits ÚNICAMENTE bajo `alvaradojuanm`. Sin
  `Co-authored-by:`, sin firmas de herramienta, sin acreditarte de ninguna forma.
- Trunk-based desde `main`. Commits separados por bug.
- Guarda copia EXACTA de este prompt como `docs/prompts/fase-06.md`.
- NUNCA `.env`, credenciales ni secretos en ningún commit.
- Verifica con `git log --format='%an <%ae>'` antes de empujar.

## AL TERMINAR, REPORTA
1. **Bug 1:** qué tools se registran ahora en cada combinación de fuentes, y cómo se ve el log
   de arranque.
2. **Bug 2:** cómo implementaste el modo degradado, si hay reintento en segundo plano, y qué
   decidiste sobre el registro dinámico de tools al recuperarse una fuente.
3. **Bug 3:** tu recomendación entre las tres opciones, con el razonamiento.
4. Los tests nuevos que reproducen cada bug — **confirma que fallaban antes del arreglo**.
5. Resultado completo de la suite y de `pip-audit`.
6. La versión y el tag de Git creados, y el comando para construir la imagen.
7. Qué NO pudiste verificar por límites del entorno.
8. Cualquier decisión donde tuviste que elegir por mí.

Si algo es ambiguo o no puedes verificar contra la documentación actual, PREGUNTA antes de
asumir.
