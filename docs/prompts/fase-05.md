Continuamos el proyecto mcp-corp. Las FASES 1 a 4 están CERRADAS y mergeadas en `main`. El server
funciona de punta a punta: 3 tools sobre Postgres y una API REST, capa de conectores con resiliencia
por fuente, auditoría con HMAC, y escalado horizontal verificado (3 réplicas × pool 3 → 9 conexiones
estables, 470/470 tool-calls completadas en shutdown).

Esta es la FASE 5: **artefactos de despliegue para entorno pre-productivo**. La imagen
`mcp-corp:1.0.1` ya está construida y cargada en el servidor destino (vía `docker save` / `docker
load`, sin registro). Falta lo que la orquesta.

NO implementes features nuevas de negocio. Esta fase produce archivos de despliegue, la corrección
de secretos, y documentación operativa.

═══════════════════════════════════════════════════════════════════════
BLOQUE 1 — CORRECCIÓN CRÍTICA: SECRETOS DE DOCKER SWARM
═══════════════════════════════════════════════════════════════════════

**El problema, y resuélvelo ANTES de escribir los archivos de despliegue.**

Docker Swarm monta los secretos como **archivos** en `/run/secrets/<nombre>`, no como variables de
entorno. Nuestro `config.py` usa pydantic-settings leyendo variables de entorno. Resultado: en
Swarm, el server no encontrará el DSN de Postgres ni `MCP_CORP_AUDIT_HMAC_SECRET`, y con
`environment=production` el fail-closed impedirá que arranque.

**Elige e implementa la solución que consideres mejor**, justificándola:
- Configurar pydantic-settings con `secrets_dir` apuntando a `/run/secrets`, o
- Soportar variables con sufijo `_FILE` (patrón común: `MCP_CORP_AUDIT_HMAC_SECRET_FILE=/run/secrets/x`)
- O una combinación

Requisitos de la solución:
- Debe seguir funcionando igual en desarrollo local con `.env` (no rompas el flujo actual).
- Debe cubrir **todos** los valores sensibles: DSN de Postgres, secreto HMAC de auditoría, y
  cualquier credencial de la API de saldos.
- Tests que verifiquen que la carga desde archivo funciona y que tiene la precedencia correcta
  respecto a variables de entorno.
- Documenta el mecanismo en el README.

═══════════════════════════════════════════════════════════════════════
BLOQUE 2 — STACK FILE PARA DOCKER SWARM (Portainer)
═══════════════════════════════════════════════════════════════════════

Crea `deploy/swarm/mcp-corp-stack.yml`, pensado para **Portainer > Stacks > Add stack > Upload**.

**Sigue estas convenciones del proyecto** (son las que ya usamos en otros despliegues):

- Cabecera con comentarios listando TODOS los prerequisitos: qué secretos crear en Portainer, qué
  redes externas deben existir, qué directorios crear en el host si aplica.
- `version: "3.8"`
- Bloque `deploy` completo: `replicas`, `restart_policy` (condition, delay, max_attempts),
  `update_config` (parallelism, delay, **order: start-first**), y `resources` con `limits` y
  `reservations` de CPU y memoria.
- Labels de Traefik para HTTPS: router principal con `entrypoints=websecure`, `tls=true`,
  `certresolver=letsencrypt`, `loadbalancer.server.port` apuntando al puerto del server (8000), más
  el router HTTP con redirección a HTTPS.
- `traefik.docker.network=traefik_public`
- Redes externas: `traefik_public` (para Traefik) y una red interna para alcanzar Postgres.
- `healthcheck` explícito.
- `logging` con `json-file`, `max-size: "10m"`, `max-file: "3"`.
- Secretos declarados como `external: true`.

**Parámetros que NO están decididos** — déjalos como placeholders CLARAMENTE marcados con `TODO` y
documentados en la cabecera, para que yo los complete:
- Dominio del router de Traefik.
- Nombre de la red interna que alcanza Postgres.
- DSN de Postgres (va por secreto, pero documenta el formato esperado).
- URL de la API de saldos.

**Sobre el `healthcheck`:** revisa qué usa el `HEALTHCHECK` del Dockerfile actual y sé consistente.
La imagen base es `python:3.12-slim`, que **no trae `curl` ni `wget`** — si el Dockerfile usa algo
que sí está disponible, usa el mismo mecanismo en el stack file. Verifícalo, no lo asumas.

**Importante sobre la imagen:** `image: mcp-corp:1.0.1` sin registro, porque se cargó manualmente
con `docker load`. Documenta en la cabecera que **si el Swarm tiene más de un nodo, la imagen debe
cargarse en TODOS los nodos**, o las réplicas quedarán en `Pending` sin error claro.

**Réplicas:** arranca en 2 y documenta cómo subirlas desde Portainer. Añade un comentario con la
fórmula de capacidad (`límite por réplica = techo de la fuente ÷ nº de réplicas`) y recuerda el
ajuste medido: con `max_connections=100` de Postgres, trabajar sobre **~85 conexiones útiles**, no
100, porque hay reservas de superusuario y otros consumidores.

### El stub de la API de saldos
Hoy vive solo en el compose de desarrollo. Inclúyelo como un **segundo servicio opcional** en el
stack file (comentado o en un archivo aparte, tú decides qué es más limpio), para que el entorno
pre-productivo pueda demostrar la tool compuesta de punta a punta. Documenta que es un stub y que
NO debe usarse en producción real.

═══════════════════════════════════════════════════════════════════════
BLOQUE 3 — MANIFIESTOS PARA KUBERNETES / OPENSHIFT
═══════════════════════════════════════════════════════════════════════

En la Fase 4 se creó un esqueleto en `deploy/openshift/`. **Complétalo hasta que sea desplegable**,
no un placeholder:

- `Deployment` con réplicas, `resources` (requests y limits), y estrategia de rolling update.
- **Sondas separadas:** `livenessProbe` → `/health`, `readinessProbe` → `/ready`. Esta separación es
  la razón por la que se diseñaron distintos desde la Fase 1; los valores de `initialDelaySeconds`,
  `periodSeconds` y `failureThreshold` deben ser coherentes con el arranque real del server.
- `Service`.
- `Secret` (plantilla, sin valores reales) y `ConfigMap` para la config no sensible.
- `Route` de OpenShift **o** `Ingress` de Kubernetes — incluye ambos y documenta cuál usar según el
  destino.
- `securityContext` con usuario no-root, coherente con el `mcpcorp` uid/gid 1000 del Dockerfile.
  **OpenShift asigna UIDs arbitrarios por defecto** — verifica si nuestra imagen es compatible con
  eso o si hace falta ajustar algo, y documéntalo.
- Opcionalmente un `HorizontalPodAutoscaler` comentado, para mostrar la ruta de autoescalado.

Comenta cada bloque explicando qué hace y qué hay que ajustar.

═══════════════════════════════════════════════════════════════════════
BLOQUE 4 — RUNBOOK DE DESPLIEGUE
═══════════════════════════════════════════════════════════════════════

Crea `deploy/README.md` (o una sección en el README principal, lo que quede más limpio) con el
procedimiento operativo completo:

1. **Construcción y transporte de la imagen sin registro:** `docker build` → `docker save` →
   transferencia → `docker load`, con los comandos exactos. Incluye la advertencia de los flags
   `--provenance=false --sbom=false` para evitar que BuildKit genere manifest lists que compliquen
   el `docker load`.
2. **Creación de los secretos** en Portainer/Swarm, uno por uno, con el formato esperado de cada
   valor.
3. **Despliegue del stack** y qué verificar después: que las réplicas estén `Running`, que
   `/health` responda, que `/ready` responda, que `/diagnostics` muestre el conector Postgres sano.
4. **Cómo escalar réplicas** y qué observar al hacerlo.
5. **Cómo hacer un rollback** a una versión anterior de la imagen.
6. **Troubleshooting**: los fallos más probables y su causa. Como mínimo: réplicas en `Pending`
   (imagen ausente en un nodo), server que no arranca (secreto HMAC faltante en modo producción),
   Traefik que no rutea (red o labels), y conector Postgres con circuito abierto (credenciales o red).
7. **La advertencia de la sesión SSE:** en cada rolling update los clientes MCP conectados pierden
   la sesión y deben reconectar. Es comportamiento conocido y medido en la Fase 4; los clientes
   necesitan lógica de reconexión.

═══════════════════════════════════════════════════════════════════════

## ANTES DE ESCRIBIR
- Revisa el `Dockerfile` actual (puerto expuesto, HEALTHCHECK, usuario, entrypoint) — todo debe ser
  coherente entre Dockerfile, stack file y manifiestos.
- Revisa `config.py` para inventariar TODAS las variables y cuáles son sensibles.
- Verifica la sintaxis vigente de labels de Traefik v2/v3 para Swarm y de probes en Kubernetes.

## LECCIONES DE LAS FASES ANTERIORES — aplícalas
1. **"El build pasa" ≠ "funciona".** Un archivo YAML sintácticamente válido puede fallar al
   desplegar. Valida lo que puedas (`docker stack config`, `kubectl apply --dry-run=client`).
2. Si NO puedes verificar algo por límites de tu entorno (no tienes un Swarm ni un cluster real),
   **dilo explícitamente**. Yo lo despliego y validamos juntos.
3. **Reporta completo:** si el prompt lo pide, que aparezca en el reporte.
4. Al terminar corre la auditoría:
   `uv export --no-hashes --format requirements-txt > requirements-audit.txt`
   `uvx pip-audit -r requirements-audit.txt`

## GIT
- Identidad: `alvaradojuanm` / `114210637+alvaradojuanm@users.noreply.github.com`. VERIFÍCALA.
- **POLÍTICA DE AUTORÍA OBLIGATORIA:** commits ÚNICAMENTE bajo `alvaradojuanm`. Sin
  `Co-authored-by:`, sin firmas de herramienta, sin acreditarte de ninguna forma.
- Trunk-based desde `main`. Commits atómicos: separa la corrección de secretos (Bloque 1) de los
  artefactos de despliegue.
- Guarda copia EXACTA de este prompt como `docs/prompts/fase-05.md`.
- NUNCA `.env`, credenciales ni secretos reales en ningún commit. **Los archivos de despliegue no
  deben contener ni un solo valor sensible** — todo por secretos o placeholders.
- Verifica con `git log --format='%an <%ae>'` antes de empujar.

## AL TERMINAR, REPORTA
1. **Qué solución elegiste para los secretos de Swarm y por qué.** Cómo convive con el `.env` de
   desarrollo y cuál tiene precedencia.
2. Archivos creados y su ubicación.
3. Qué placeholders `TODO` debo completar yo antes de desplegar.
4. Qué mecanismo usa el healthcheck y por qué (dado que la imagen base no trae curl ni wget).
5. Si nuestra imagen es compatible con la asignación de UIDs arbitrarios de OpenShift, o qué hay que
   ajustar.
6. Resultado de los tests y de `pip-audit`.
7. Qué NO pudiste verificar por límites del entorno.
8. Cualquier decisión donde tuviste que elegir por mí.

Si algo es ambiguo o no puedes verificar contra la documentación actual, PREGUNTA antes de asumir.
