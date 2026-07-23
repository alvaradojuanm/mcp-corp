Continuamos el proyecto mcp-corp. Las FASES 1 y 2 están CERRADAS y mergeadas en `main`:

- **Fase 1:** andamiaje FastMCP 3.4.4 sobre Streamable HTTP, stateless, config con
  pydantic-settings, logging JSON con correlation id, `/health` y `/ready` separados, graceful
  shutdown, contenedor no-root verificado.
- **Fase 2:** capa de conectores en dos niveles — `resilience.py` genérica (BoundedSemaphore por
  fuente + `asyncio.timeout()` + CircuitBreaker propio), `base.py` con el Protocol `Connector`,
  `registry.py` con el ciclo de vida y la agregación de `/diagnostics`, y `postgres.py` con
  psycopg3 + `AsyncConnectionPool`. 12/12 tests pasando contra Postgres real.

Esta es la FASE 3: **el conector HTTP y las primeras tools MCP**. Es la fase donde el agente MCP
por fin ve algo y donde se cierra el camino completo agente → tool → conector → dato.

## El norte de esta fase
Demostrar el **patrón heterogéneo**: fuentes de naturaleza distinta (una base de datos con driver
nativo, una API REST por HTTP) quedando detrás de tools homogéneas. El agente que consuma el server
no debe poder distinguir de dónde sale cada dato. Toda la heterogeneidad vive encapsulada en la
capa de conectores.

Y de paso, **validar que la capa de resiliencia de la Fase 2 sirve para algo que no es Postgres**.
Se diseñó genérica; si nunca la usa una segunda fuente, esa abstracción no está probada.

## ALCANCE EXACTO

1. **Conector HTTP** (`src/mcp_corp/connectors/http.py` o similar) — cumpliendo el mismo Protocol
   `Connector` de `base.py`, envuelto por la MISMA capa de resiliencia. Debe ser notablemente más
   simple que el de Postgres (no hay pool que gestionar); si te resulta complicado, es señal de que
   la abstracción de la Fase 2 tiene un hueco: **dímelo en vez de forzarlo**.
2. **Un stub de API REST** en `docker-compose.dev.yml` para probar el conector HTTP de verdad
   (algo mínimo y determinista, con endpoints de saldo por identificador).
3. **Tres tools MCP** (detalle abajo).
4. **Las otras dos primitivas MCP:** un **Resource** y un **Prompt**.
5. **Logging de auditoría por invocación** — el correlation id lleva dos fases esperando su
   propósito; aquí lo cumple.

## LAS TRES TOOLS

| Tool | Fuente | Naturaleza |
|---|---|---|
| `consultar_cliente` | PostgreSQL | driver nativo, SQL parametrizado |
| `consultar_saldo` | API REST (stub) | HTTP |
| `resumen_cliente` | ambas | compuesta: orquesta las dos por dentro |

Apóyate en los datos semilla que ya existen (`deploy/dev/postgres-seed.sql`); extiéndelos si hace
falta y refleja los mismos identificadores en el stub de la API para que la tool compuesta cuadre.

**Disciplina de tools — no negociable.** Tools por INTENCIÓN DE NEGOCIO, no una por endpoint.
Pocas y bien nombradas. Un server MCP conocido volcó 43 tools en el contexto del modelo y destrozó
su rendimiento: el modelo elige mal cuando tiene demasiadas opciones parecidas. Tres tools bien
definidas valen más que quince granulares.

**Las descripciones y los esquemas los lee un LLM, no un humano.** Son la interfaz real del
server. Escríbelas pensando en que un modelo decida correctamente CUÁNDO usar cada tool: descripción
clara del propósito, parámetros con descripción y formato explícito (p. ej. formato de cédula),
sin ambigüedad entre tools parecidas. Este es el punto donde más se gana o se pierde calidad.

## DECISIONES DE DISEÑO — cerradas, no las reabras

**Tool compuesta: resultado parcial explícito, no fallo total.**
Si `resumen_cliente` obtiene los datos del cliente pero la API de saldo falla (o su circuito está
abierto), NO falles la tool entera. Devuelve lo que sí obtuviste, marcando de forma INEQUÍVOCA qué
parte no está disponible y por qué (en términos de negocio, no técnicos). El modelo debe poder
decirle al usuario "tengo los datos del cliente pero el saldo no está disponible ahora mismo" en
vez de quedarse sin nada. Lo que no puede pasar nunca es que el modelo crea que un dato faltante es
un cero o un dato válido.

**Concurrencia en la tool compuesta: `asyncio.TaskGroup`, NO `asyncio.gather`.**
`gather` deja tareas huérfanas corriendo cuando una falla. Consulta ambas fuentes en paralelo, no
en secuencia.

**Errores hacia el modelo: limpios y accionables, nunca internals.**
Jamás un stack trace, un string de conexión, un fragmento de SQL, un nombre de host interno ni un
detalle del esquema de base de datos deben llegar al modelo. Un circuito abierto se traduce como
"esta información no está disponible temporalmente", no como el nombre de la excepción. El detalle
técnico va al log, no a la respuesta.

**Logging de auditoría — con cuidado de datos sensibles.**
Cada invocación de tool debe quedar registrada en el log JSON estructurado con: correlation id,
nombre de la tool, marca de tiempo, duración, y resultado (éxito / fallo / parcial, y la causa).

Pero **NO vuelques payloads completos ni datos personales en claro**. Un log con cédulas, nombres y
saldos de clientes es un problema de cumplimiento en sí mismo, y estos logs van a un agregador
externo. Registra el HECHO de la llamada y su resultado; para los parámetros, usa enmascaramiento u
omisión. Documenta el criterio que elijas — este punto lo va a revisar un auditor algún día.

## Resource y Prompt

- **Resource:** un dato de solo lectura que la aplicación cliente pueda leer sin invocar una acción
  (por ejemplo, un catálogo de productos o el diccionario de códigos de estado). Elige algo que
  tenga sentido de negocio y explica en el README la diferencia entre Resource y Tool: la Tool es
  una acción que el modelo decide invocar; el Resource es contexto que la aplicación puede cargar.
- **Prompt:** una plantilla de workflow reutilizable (por ejemplo, un flujo de atención al cliente
  que use las tools en cierto orden). Sirve para demostrar la primitiva y dejar el manual completo.

## ANTES DE ESCRIBIR CÓDIGO
- Verifica en la documentación vigente de FastMCP 3.x la API actual para registrar **Tools**,
  **Resources** y **Prompts**, y cómo se declaran descripciones y esquemas de parámetros. La 3.0 fue
  una reescritura mayor; no te fíes de tu memoria.
- Verifica la versión estable actual de `httpx` y pínéala exacta.
- Revisa `base.py`, `resilience.py` y `registry.py` de la Fase 2 antes de escribir el conector HTTP:
  **reutiliza**, no dupliques.

## LECCIONES DE LAS FASES ANTERIORES — aplícalas
1. **"El build pasa" ≠ "funciona".** En la Fase 1 un bug pasó el build limpio y reventó al arrancar
   el contenedor. Verifica que las cosas CORRAN de punta a punta.
2. Si NO puedes verificar algo por límites de tu entorno, **dilo explícitamente** en el reporte en
   vez de asumir que funciona.
3. **Reporta lo que hiciste, completo.** En la Fase 2 implementaste un requisito en cuatro lugares
   pero no lo mencionaste en el resumen, y tuve que preguntarte. Si el prompt lo pide, que aparezca
   en el reporte.
4. Al terminar corre la auditoría:
   `uv export --no-hashes --format requirements-txt > requirements-audit.txt`
   `uvx pip-audit -r requirements-audit.txt`

## TESTS
- Unitarios del conector HTTP con un servidor falso o transporte mock: verifica que la resiliencia
  lo envuelve de verdad (timeout dispara, breaker abre al umbral, semáforo limita).
- Integración de las tres tools contra Postgres real + el stub de API levantados con
  `docker-compose.dev.yml`.
- Un test específico de la tool compuesta con **una fuente caída**, verificando que devuelve
  resultado parcial correctamente marcado y no falla entera.
- Un test de que los errores hacia el modelo NO contienen internals.

## DOCUMENTACIÓN (README)
- Las tres tools con su propósito, parámetros y qué fuente consume cada una.
- La diferencia entre Tool, Resource y Prompt, con los ejemplos implementados.
- Por qué tools por intención de negocio y no por endpoint.
- La política de resultado parcial en la tool compuesta.
- El criterio de enmascaramiento en el log de auditoría.
- Cómo probar todo en local de punta a punta (levantar el compose, arrancar el server, invocar las
  tools).

## GIT
- Identidad: `alvaradojuanm` / `114210637+alvaradojuanm@users.noreply.github.com`. VERIFÍCALA antes
  de commitear y corrígela si el entorno la reseteó.
- **POLÍTICA DE AUTORÍA OBLIGATORIA:** commits ÚNICAMENTE bajo `alvaradojuanm`. Sin
  `Co-authored-by:`, sin firmas de herramienta, sin acreditarte de ninguna forma.
- Trunk-based: parte de `main` actualizado. Commits atómicos y descriptivos.
- Guarda copia EXACTA de este prompt como `docs/prompts/fase-03.md`.
- NUNCA `.env`, credenciales ni secretos en ningún commit.
- Verifica con `git log --format='%an <%ae>'` antes de empujar.

## AL TERMINAR, REPORTA
1. Versiones exactas pineadas (httpx y cualquier dependencia nueva).
2. Archivos creados y estructura resultante.
3. Las tres tools con su firma final (nombre, descripción, parámetros) tal como las ve un modelo.
4. Resultado de todos los tests, incluido el de fuente caída y el de no-filtración de internals.
5. Qué criterio usaste para el enmascaramiento de datos sensibles en el log de auditoría.
6. Resultado de `pip-audit`.
7. Qué NO pudiste verificar por límites del entorno.
8. Cualquier decisión donde tuviste que elegir por mí.
9. **Si el conector HTTP te resultó forzado sobre la abstracción de la Fase 2, dímelo** — es
   información valiosa sobre un hueco de diseño, no un fracaso.

Esta fase termina con las tres tools, el Resource y el Prompt funcionando de punta a punta.

Si algo es ambiguo o no puedes verificar contra la documentación actual, PREGUNTA antes de asumir.
Prefiero corregir el rumbo ahora que rehacer después.
