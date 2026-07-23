Continuamos el proyecto mcp-corp. Las FASES 1, 2 y 3 están CERRADAS y mergeadas en `main`:

- **Fase 1:** andamiaje FastMCP 3.4.4 sobre Streamable HTTP, stateless, config con
  pydantic-settings, logging JSON con correlation id, `/health` y `/ready` separados, graceful
  shutdown, contenedor no-root.
- **Fase 2:** capa de conectores en dos niveles — `resilience.py` genérica (BoundedSemaphore por
  fuente + `asyncio.timeout()` + CircuitBreaker propio), `base.py` con el Protocol `Connector`,
  `registry.py` con ciclo de vida y `/diagnostics`, y `postgres.py` con psycopg3.
- **Fase 3:** `http.py` (conector HTTP), `tools.py` (3 tools + Resource + Prompt), `audit.py`
  (auditoría por invocación con enmascaramiento HMAC-SHA256). 44/44 tests.

Esta es la FASE 4 y tiene **DOS PARTES INDEPENDIENTES**. Complétalas EN ORDEN: termina la Parte A
por completo (con sus tests pasando) antes de empezar la Parte B. No mezcles los commits de ambas.

═══════════════════════════════════════════════════════════════════════
PARTE A — NORMALIZACIÓN Y VALIDACIÓN DE IDENTIFICADORES VENEZOLANOS
═══════════════════════════════════════════════════════════════════════

## El problema
Las tres tools actuales aceptan `cedula: Annotated[str, Field(pattern=r"^\d{6,10}$")]` — solo
dígitos. Pero en Venezuela los identificadores se escriben de muchas formas y el modelo va a pasar
lo que el usuario escribió, no lo que nuestro regex espera. Si el usuario dice "consúltame la
cédula V-16.760.320", el modelo tiene que adivinar qué mandar, y si adivina mal la tool falla antes
de tocar ninguna fuente.

**La normalización va en el código, no en el modelo.** El esquema debe aceptar formato flexible y
el server normaliza internamente.

## Formatos que deben aceptarse como equivalentes

```
Cédula:  16760320      16.760.320      V16760320
         V-16760320    V-16.760.320
RIF:     J-167603200   J16.760.320-0   J-16760320-0
```

Con o sin puntos de millar, con o sin guiones, con o sin letra, mayúscula o minúscula.

## Reglas del dominio (VERIFÍCALAS, no las asumas)

- **RIF:** 10 caracteres — una letra + 8 dígitos + 1 dígito verificador (checksum).
- **Cédula:** 8 dígitos, SIN dígito verificador.
- Para personas naturales con letra `V`, los 8 dígitos del RIF **son** el número de cédula.
- **Prefijos válidos: `V`, `E`, `J`, `G`, `P`.**
  - `V` = persona natural venezolana · `E` = persona natural extranjera ·
    `J` = persona jurídica · `G` = entidad gubernamental · `P` = titular de pasaporte
  - **`C`** existe para comunas, consejos comunales y organizaciones del Poder Popular, pero las
    fuentes difieren sobre si sigue en el set oficial del SENIAT. **Inclúyela como configurable
    (activable por config), no cableada**, y documenta la ambigüedad.

### ⚠️ TRAMPA CONOCIDA — no la repitas
**La letra `I` NO EXISTE en el registro oficial del SENIAT.** El prefijo correcto para extranjeros
es `E`. Muchas librerías de validación y patrones de regex publicados heredaron el `I` erróneo de
fuentes no oficiales. Si tu memoria o un ejemplo que encuentres incluye `I`, está mal. NO la
incluyas.

### Relleno con cero
El portal del SENIAT indica que si el número es menor a nueve dígitos, se antepone un cero.
Contempla esto en la normalización.

## El dígito verificador — trátalo con cuidado
El SENIAT usa una fórmula de checksum documentada (históricamente llamada "número de chequeo") para
el décimo carácter del RIF. Validarlo permite **rechazar identificadores mal tipeados ANTES de
tocar ninguna fuente**: sin consumir una conexión del pool, sin gastar un slot del semáforo, sin
llegar al core bancario. En un sistema donde cada fuente tiene un techo de concurrencia, ese
filtrado temprano es justo el tipo de protección que venimos construyendo.

**PERO:** si implementas mal el algoritmo, vas a rechazar RIFs válidos — un fallo peor que no
validar nada.

Por lo tanto:
1. **Verifica el algoritmo contra fuentes** antes de escribirlo. No lo reconstruyas de memoria.
2. **Pruébalo contra RIFs reales conocidos como válidos** (busca ejemplos verificables; no inventes
   casos de prueba a partir de tu propia implementación — eso solo prueba que el código es
   consistente consigo mismo).
3. **Si NO logras verificarlo con confianza, dímelo y NO lo actives.** Implementa la normalización
   (que es lo importante) y deja la validación de checksum detrás de un flag desactivado por
   defecto, documentando que está sin verificar. Prefiero eso mil veces a un validador que rechaza
   clientes reales.

## Implementación
- Módulo dedicado (p. ej. `src/mcp_corp/identifiers.py`) — NO metas esto dentro de `tools.py`.
- Una función de normalización que devuelva una forma canónica estructurada: tipo de documento,
  letra/prefijo, número base, dígito verificador (si aplica).
- Que cada conector decida qué forma canónica necesita su fuente: Postgres puede almacenar
  `V16760320` y el core bancario esperar otra cosa. La tool normaliza; el conector adapta.
- Actualiza las tres tools para usar el módulo. El esquema que ve el modelo debe aceptar formato
  flexible, con la descripción del parámetro explicando que admite varios formatos (para que el
  modelo no se sienta obligado a limpiar la entrada).
- Identificador inválido → error de negocio claro hacia el modelo ("identificador inválido"), sin
  filtrar internals, y **sin consumir recursos de ninguna fuente**.

## Tests de la Parte A
- Tabla de casos con todos los formatos equivalentes de arriba, verificando que normalizan al mismo
  valor canónico.
- Prefijos válidos aceptados; **`I` rechazada explícitamente** (test de regresión de la trampa).
- Cédula vs RIF correctamente distinguidos.
- Relleno con cero.
- Entradas basura rechazadas sin tocar conectores (verifica que no se consume slot del semáforo).
- Si activas el checksum: casos válidos e inválidos verificados contra ejemplos externos.

═══════════════════════════════════════════════════════════════════════
PARTE B — DESPLIEGUE Y ESCALADO HORIZONTAL
═══════════════════════════════════════════════════════════════════════

## El objetivo
Demostrar la tesis central del diseño: **más carga se atiende con más réplicas, sin tocar el
código.** Y verificar bajo tráfico real las cosas que hasta ahora solo asumimos.

## Qué construir
- **Compose de Swarm listo para escalar** (`deploy/swarm/docker-compose.yml`, ya existe): revísalo
  y ajústalo para que `deploy.replicas` funcione de verdad con las labels de Traefik balanceando
  entre réplicas. Documenta cómo subir/bajar réplicas desde Portainer.
- **Esqueleto de manifiestos para OpenShift/Kubernetes** en `deploy/openshift/` (hoy es un
  placeholder): Deployment con `replicas`, Service, y sondas de **liveness apuntando a `/health`**
  y **readiness apuntando a `/ready`** — que es la razón por la que los separamos desde la Fase 1.
  No hace falta que sea desplegable en un cluster real, pero sí correcto y comentado.
- **Un script o receta de prueba de carga** reproducible (algo simple: `hey`, `locust`, o un script
  async propio) que permita generar concurrencia contra las tools.

## Qué VERIFICAR bajo carga — esto es lo importante de la fase

Recuerda la lección de la Fase 3: **lo que se cablea pero no se usa, no se valida.** El
`JSONFormatter` estuvo dos fases roto porque nada lo consumía. Varias cosas del diseño están en esa
misma situación y esta fase es la que las ejercita por primera vez:

1. **La fórmula de capacidad.** `límite por réplica = techo de la fuente ÷ nº de réplicas`.
   Verifica empíricamente: con N réplicas y límite L por réplica, confirma que las conexiones
   concurrentes contra Postgres nunca superan N×L. Documenta el resultado con números reales.
2. **El presupuesto de conexiones.** N réplicas × tamaño de pool contra el `max_connections` de
   Postgres. Calcula el punto donde se rompe y **documenta el umbral** a partir del cual haría
   falta PgBouncer.
3. **El graceful shutdown durante una rotación de réplicas** (rolling update). Verifica que una
   réplica que se está apagando deja de recibir tráfico nuevo (readiness cae) y termina las
   peticiones en curso sin cortarlas. Esta secuencia nunca se ha probado con tráfico real.
4. **`/ready` bajo carga.** Confirma que sigue respondiendo correctamente cuando el server está
   saturado, y que NO se degrada por la salud de las fuentes (decisión de la Fase 2).
5. **El circuit breaker con múltiples réplicas.** Cada réplica tiene su propio estado. Verifica que
   una fuente caída se descubre de forma independiente en cada una, y documenta qué implica eso en
   la práctica (p. ej. la fuente recibe N intentos de sondeo en medio-abierto, uno por réplica).
6. **Comportamiento al saturar el semáforo.** Cuando el límite de concurrencia está lleno, ¿qué
   experimenta el cliente? Confirma que espera o falla limpio, y que nunca se encola infinito.

## Pendiente heredado a resolver en esta parte
**Fail-closed sin clave HMAC.** Hoy, si `MCP_CORP_AUDIT_HMAC_SECRET` viene vacía, el server registra
un warning y arranca igual. HMAC con clave vacía es determinista y públicamente reproducible — o
sea, el enmascaramiento no protege nada. Introduce un concepto de **modo producción** (p. ej. una
variable de entorno) y haz que en ese modo el server **NO ARRANQUE** sin clave. En desarrollo, el
warning actual está bien.

## Documentación (README)
- Cómo escalar réplicas en Swarm/Portainer y qué observar.
- **Los números reales medidos** en las verificaciones de arriba — no teoría. Esta sección es la
  evidencia de que el diseño escala.
- El umbral documentado a partir del cual hace falta PgBouncer.
- La ruta de migración a OpenShift: qué cambia (solo la capa de orquestación) y qué no (el código).
- Los formatos de identificador aceptados y el criterio de normalización (Parte A).

═══════════════════════════════════════════════════════════════════════

## ANTES DE ESCRIBIR CÓDIGO
- Verifica las reglas del RIF/cédula contra fuentes (Parte A), especialmente el algoritmo del dígito
  verificador y el set de prefijos.
- Revisa `identifiers` no existe aún; `tools.py`, `base.py`, `resilience.py`, `registry.py` sí —
  reutiliza, no dupliques.
- Verifica la sintaxis vigente de `deploy.replicas` y labels de Traefik para Swarm, y la estructura
  actual de probes en manifiestos de Kubernetes/OpenShift.

## LECCIONES DE LAS FASES ANTERIORES — aplícalas
1. **"El build pasa" ≠ "funciona".** Verifica que las cosas CORRAN de punta a punta.
2. **Lo que se cablea pero no se usa, no se valida.** Es literalmente el tema de la Parte B.
3. Si NO puedes verificar algo por límites de tu entorno (p. ej. no puedes levantar un Swarm real),
   **dilo explícitamente** en vez de asumir. Yo lo valido en mi máquina.
4. **Reporta completo.** Si el prompt lo pide, que aparezca en el reporte.
5. Al terminar corre la auditoría:
   `uv export --no-hashes --format requirements-txt > requirements-audit.txt`
   `uvx pip-audit -r requirements-audit.txt`

## GIT
- Identidad: `alvaradojuanm` / `114210637+alvaradojuanm@users.noreply.github.com`. VERIFÍCALA antes
  de commitear.
- **POLÍTICA DE AUTORÍA OBLIGATORIA:** commits ÚNICAMENTE bajo `alvaradojuanm`. Sin
  `Co-authored-by:`, sin firmas de herramienta, sin acreditarte de ninguna forma.
- Trunk-based desde `main` actualizado. **Commits de la Parte A y la Parte B separados.**
- Guarda copia EXACTA de este prompt como `docs/prompts/fase-04.md`.
- NUNCA `.env`, credenciales ni secretos en ningún commit.
- Verifica con `git log --format='%an <%ae>'` antes de empujar.

## AL TERMINAR, REPORTA

**Parte A:**
1. Formatos soportados y forma canónica resultante.
2. **¿Lograste verificar el algoritmo del dígito verificador? ¿Contra qué fuente? ¿Lo activaste o
   lo dejaste tras un flag?** Sé explícito — esta es la pregunta más importante de la Parte A.
3. Resultado de los tests, incluido el de regresión de la letra `I`.
4. Qué decidiste sobre el prefijo `C`.

**Parte B:**
5. Los números reales medidos en cada verificación (fórmula de capacidad, presupuesto de
   conexiones, umbral de PgBouncer, comportamiento en rotación y bajo saturación).
6. Qué NO pudiste verificar por límites del entorno.
7. Cómo implementaste el modo producción / fail-closed del HMAC.

**Ambas:**
8. `pip-audit`.
9. Cualquier decisión donde tuviste que elegir por mí.
10. Si algo del diseño existente te estorbó o te resultó forzado, dímelo — es información valiosa.

Si algo es ambiguo o no puedes verificar contra fuentes, PREGUNTA antes de asumir. Prefiero corregir
el rumbo ahora que rehacer después.
