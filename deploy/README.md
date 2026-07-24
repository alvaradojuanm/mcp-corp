# Runbook de despliegue — entorno pre-productivo (Fase 5)

Procedimiento operativo completo para llevar `mcp-corp` a un Docker Swarm
sin registro de imágenes (transporte manual vía `docker save`/`docker
load`). Para OpenShift/Kubernetes ver [`deploy/openshift/README.md`](openshift/README.md)
— ese documento cubre la parte específica de esa plataforma; este runbook
se enfoca en Swarm/Portainer, que es el destino inmediato de esta fase.

## 1. Construcción y transporte de la imagen (sin registro)

```bash
# Construir. --provenance=false --sbom=false evitan que BuildKit genere
# un manifest LIST (multi-plataforma) en vez de una imagen simple — un
# manifest list rompe `docker load` en el nodo destino con un error poco
# claro ("no matching manifest") si la arquitectura no coincide
# exactamente. Con estos flags, `docker save` exporta una imagen de una
# sola plataforma, la del host donde se construyó.
docker build --provenance=false --sbom=false -t mcp-corp:1.0.2 .

# Exportar a un .tar (puede pesar ~80-100 MB con esta imagen).
docker save -o mcp-corp-1.0.2.tar mcp-corp:1.0.2

# Transferir el .tar al/los nodo(s) destino (scp, un share de red, un USB
# — lo que aplique en tu entorno). Ejemplo con scp:
scp mcp-corp-1.0.2.tar usuario@nodo-swarm:/tmp/

# En CADA nodo del swarm donde el scheduler pueda colocar una réplica:
docker load -i /tmp/mcp-corp-1.0.2.tar
docker images mcp-corp   # confirma que quedó cargada
```

> **`mcp-corp-1.0.2.tar` NUNCA debe ir al repositorio de git** — son
> decenas de MB de binario. Ya está excluido en `.gitignore` (`*.tar`).

⚠️ **Si el swarm tiene más de un nodo, repite `docker load` en TODOS.**
Docker Swarm no distribuye imágenes locales entre nodos — si falta en
alguno, las réplicas que el scheduler intente colocar ahí quedan en
`Pending` sin ningún mensaje de error que lo diga explícitamente (ver
"Troubleshooting" más abajo).

## 2. Creación de los secretos (uno por uno)

Docker Swarm monta los secretos como archivos en `/run/secrets/<nombre>`
dentro del contenedor; `config.py` los resuelve vía el patrón
`MCP_CORP_*_FILE` (ver README raíz, sección "Secretos como archivo").

```bash
# HMAC de auditoría: genera un valor largo y aleatorio, NUNCA lo escribas
# en un archivo de despliegue ni en un commit.
python -c "import secrets; print(secrets.token_hex(32))" | \
  docker secret create mcp_corp_audit_hmac_secret -

# DSN de Postgres: cadena de conexión libpq completa.
printf '%s' 'postgresql://usuario:password@host:5432/basededatos' | \
  docker secret create mcp_corp_postgres_dsn -

# Verificar que quedaron creados (no muestra el valor, solo metadatos):
docker secret ls
```

Si algún secreto necesita rotarse: los secretos de Swarm son inmutables
una vez creados. Hay que crear uno NUEVO con otro nombre (p. ej.
`mcp_corp_audit_hmac_secret_v2`), actualizar `mcp-corp-stack.yml` para
que apunte al nuevo `source:`, y volver a desplegar el stack — no se
puede editar un secreto existente in-place.

> Rotar `mcp_corp_audit_hmac_secret` rompe la correlación histórica del
> log de auditoría a propósito — comportamiento esperado, ver Fase 3.

## 3. Despliegue del stack — EN DOS PASOS, no todo de una

`mcp-corp-stack.yml` arranca con `MCP_CORP_SALDO_API__ENABLED: "false"`
a propósito. Si activas Postgres y la API de saldos al mismo tiempo y
algo falla, no sabes de cuál lado está — y la URL de saldos hoy es un
placeholder que de todas formas no respondería. Verifica un lado antes de
prender el otro.

**Paso 3a — Postgres solo:**

```bash
docker stack deploy -c deploy/swarm/mcp-corp-stack.yml mcp-corp
```

O desde Portainer: **Stacks → Add stack → Upload**, sube
`deploy/swarm/mcp-corp-stack.yml`.

**Verificación** (dale unos segundos a que las réplicas arranquen y pasen
el `start_period` del healthcheck):

```bash
# 1. Las réplicas están Running, no Pending ni Restarting.
docker service ps mcp-corp_mcp-corp

# 2. /health responde (liveness: el proceso está vivo).
curl -i https://TODO-mcp-corp.example.com/health

# 3. /ready responde (readiness: terminó de arrancar, no se está apagando).
curl -i https://TODO-mcp-corp.example.com/ready

# 4. /diagnostics muestra el conector Postgres sano
#    (circuit_state: "closed", healthy: true). saldo_api todavía no
#    debe aparecer — sigue deshabilitado.
curl -s https://TODO-mcp-corp.example.com/diagnostics | jq
```

Si el paso 1 falla (réplicas no llegan a `Running`) o el 2 falla (ni
siquiera `/health` responde), ver "Troubleshooting" abajo antes de seguir.

**Paso 3b — activar la API de saldos**, solo después de que 3a esté sano:

1. Resuelve el TODO de `MCP_CORP_SALDO_API__BASE_URL` en
   `mcp-corp-stack.yml` con la URL real (o la del stub, ver
   `saldo-api-stub-stack.yml`, si es solo para demo).
2. Cambia `MCP_CORP_SALDO_API__ENABLED` a `"true"`.
3. Vuelve a desplegar: `docker stack deploy -c deploy/swarm/mcp-corp-stack.yml mcp-corp`.
4. Repite la verificación del punto 4 de arriba — ahora `/diagnostics`
   también debe mostrar `saldo_api` con `healthy: true`.

## 4. Cómo escalar réplicas

Desde Portainer: **Stacks → mcp-corp → Services → mcp-corp → Scale
service**. Desde la CLI:

```bash
docker service scale mcp-corp_mcp-corp=4
```

**Qué observar al escalar (medido con réplicas reales en la Fase 4):**
- Con `update_config.order: start-first`, las réplicas nuevas arrancan y
  pasan su healthcheck ANTES de que Traefik deje de enrutarle tráfico a
  las existentes — nunca hay una ventana sin capacidad.
- **Reajusta `MCP_CORP_POSTGRES__MAX_CONCURRENCY` (y el de `saldo_api`)
  cada vez que cambies el número de réplicas** — la fórmula de capacidad
  es `límite por réplica = techo de la fuente ÷ nº de réplicas`. Con
  `max_connections=100` de Postgres, trabaja sobre ~85 conexiones útiles
  reales (reservas de superusuario + otros clientes), no sobre 100 — ver
  README raíz, "Verificaciones bajo carga".
- El circuit breaker es **por réplica** (decisión de diseño, no un
  defecto): si escalas de 2 a 4 réplicas mientras una fuente está caída,
  las 2 réplicas nuevas empiezan con el circuito `closed` y lo descubren
  por su cuenta con sus propios fallos — no heredan el estado de las
  réplicas existentes.

## 5. Cómo hacer un rollback

```bash
# Si ya tienes la imagen anterior cargada en todos los nodos:
docker service update --image mcp-corp:1.0.0 mcp-corp_mcp-corp

# O edita mcp-corp-stack.yml (cambia `image:`) y vuelve a desplegar:
docker stack deploy -c deploy/swarm/mcp-corp-stack.yml mcp-corp

# Docker Swarm también puede revertir automáticamente si
# update_config.failure_action: rollback detecta que la actualización
# falló (healthcheck no pasa dentro del margen configurado). Para forzar
# un rollback manual al estado previo del servicio:
docker service rollback mcp-corp_mcp-corp
```

**Antes de un rollback a una versión anterior:** confirma que la imagen
de esa versión (`mcp-corp:X.Y.Z`) esté cargada en todos los nodos — mismo
requisito que el paso 1, y la misma forma de fallar en silencio
(`Pending`) si no lo está.

## 6. Troubleshooting

| Síntoma | Causa probable | Qué revisar |
|---|---|---|
| Réplicas en `Pending`, sin arrancar | La imagen no está cargada en el nodo donde el scheduler intentó colocarlas | `docker service ps mcp-corp_mcp-corp --no-trunc` → busca "no suitable node" o similar en la columna de error. Carga la imagen en ese nodo (paso 1) |
| El proceso no arranca / el contenedor termina inmediatamente | `MCP_CORP_ENVIRONMENT=production` sin `MCP_CORP_AUDIT_HMAC_SECRET` resuelto — fail-closed (Fase 4) | `docker service logs mcp-corp_mcp-corp` → busca el `ValidationError` de pydantic mencionando `AUDIT_HMAC_SECRET`. Confirma que el secreto existe (`docker secret ls`) y que el stack lo referencia bajo `secrets:` con el `target:` correcto |
| Traefik no rutea tráfico al servicio (404 o el dominio no resuelve) | Falta la red `traefik_public`, o las labels de Traefik no coinciden con el dominio configurado, o el certificado de Let's Encrypt no se emitió | Verifica que el servicio está en la red `traefik_public` (`docker service inspect mcp-corp_mcp-corp`), que reemplazaste el `TODO-mcp-corp.example.com` por el dominio real en TODAS las labels, y los logs del propio Traefik para errores de ACME/certresolver |
| `/diagnostics` muestra el conector Postgres con `circuit_state: "open"` o `healthy: false` | Credenciales incorrectas en el secreto `mcp_corp_postgres_dsn`, o la red interna no alcanza el host de Postgres | Revisa el DSN del secreto (formato: `postgresql://usuario:password@host:5432/db`), que el servicio esté en la red interna correcta (prerrequisito 4 del stack file), y que Postgres acepte conexiones desde esa red (`pg_hba.conf` / grupos de seguridad) |
| Réplicas se reinician en bucle (`Restarting`) | El healthcheck falla repetidamente, o la app crashea al conectar a una fuente obligatoria | `docker service logs mcp-corp_mcp-corp` para el traceback; si es un fallo de conexión a Postgres al arrancar, revisa el mismo punto que la fila anterior |

## 7. Advertencia conocida: sesiones MCP y rolling updates

**Medido con carga real en la Fase 4, no es un supuesto.** En cada rolling
update (al escalar, actualizar la imagen, o cuando Swarm reemplaza una
réplica), el stream SSE de cada sesión MCP activa en la réplica saliente
se corta al recibir SIGTERM — no espera el timeout de apagado completo.
La garantía que SÍ se sostiene: ninguna *tool call* que ya había arrancado
queda a medias (470/470 se completaron en la prueba de carga de la Fase
4); lo que se pierde es la sesión de transporte, no una operación de
negocio en curso.

**Implicación operativa:** cualquier cliente MCP que se conecte a este
server necesita lógica de reconexión — no es opcional, es parte del
contrato de correr detrás de réplicas que rotan. Si el cliente no
reconecta solo, el usuario percibe un corte durante un deploy/escalado
aunque el server esté sano.
