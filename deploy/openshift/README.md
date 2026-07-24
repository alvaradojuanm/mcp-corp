# Manifiestos de OpenShift / Kubernetes

Manifiestos completos (no un placeholder) para el destino final del
proyecto — ver README raíz, sección "Fase 1". **No se probaron contra un
cluster real** — este entorno no tenía uno disponible — pero la
estructura, las sondas, el `securityContext` y las variables de entorno
son las mismas que ya se validaron corriendo el server directamente, en
Docker y en Docker Swarm. Sintaxis YAML verificada.

Imagen: `mcp-corp:1.0.1`, la misma que se transporta sin registro (ver
`deploy/README.md` en la raíz de `deploy/`) — si tu cluster sí tiene
acceso a un registro de imágenes, es preferible subirla ahí y referenciar
esa ruta en vez de depender de que la imagen ya esté en cada nodo.

## Qué cambia al migrar desde Swarm, y qué NO

**Cambia (capa de orquestación):**
- `deploy.replicas` (Swarm) → `spec.replicas` (`deployment.yaml`).
- Las labels de Traefik → un `Service` + un `Route` (OpenShift) o `Ingress`
  (Kubernetes vanilla) — `service.yaml` y `route.yaml`.
- El healthcheck de Docker (`HEALTHCHECK` en el Dockerfile / `healthcheck:`
  en compose) → `livenessProbe` / `readinessProbe` nativas de Kubernetes,
  con sondas SEPARADAS (ver abajo) en vez de una sola.
- Variables de entorno en texto plano (`environment:` en compose) →
  `ConfigMap` (`configmap.yaml`) para lo no sensible, `Secret`
  (`secret.example.yaml`) para `MCP_CORP_AUDIT_HMAC_SECRET` y el DSN de
  Postgres.

**NO cambia (el código del server):**
- El proceso sigue siendo exactamente el mismo binario/imagen: stateless
  por invocación, `/health` y `/ready` separados, graceful shutdown ante
  SIGTERM, conectores con su propia resiliencia. Nada de eso es
  consciente de qué orquestador lo corre.
- La fórmula de capacidad (`límite por réplica = techo de la fuente ÷ nº
  de réplicas`) se sigue aplicando igual, solo que ahora `nº de réplicas`
  lo fija `spec.replicas` en vez de `deploy.replicas`.

## Por qué las sondas están separadas (la razón de ser de la Fase 1)

```yaml
livenessProbe:
  httpGet: { path: /health, port: 8000 }
readinessProbe:
  httpGet: { path: /ready, port: 8000 }
```

Es la razón por la que `/health` y `/ready` se separaron desde la Fase 1:
Kubernetes/OpenShift, a diferencia de Docker Swarm, exige sondas de
liveness y readiness independientes de verdad. Si `livenessProbe` fallara
repetidas veces, Kubernetes **reinicia el contenedor** (correcto si el
proceso está realmente colgado). Si `readinessProbe` fallara, Kubernetes
**deja de enrutarle tráfico nuevo** sin reiniciarlo (correcto durante el
arranque, el apagado, o si `/ready` reportara problemas — que nunca pasa
por salud de conectores, ver "Decisiones de diseño" en el README raíz).

## Archivos

| Archivo | Qué es |
|---|---|
| `deployment.yaml` | Deployment: réplicas, sondas, recursos, `securityContext`, variables de entorno |
| `service.yaml` | Service ClusterIP: expone el puerto 8000 dentro del cluster |
| `route.yaml` | Route de OpenShift — **usar en OpenShift**, incluido en `kustomization.yaml` |
| `ingress.yaml` | Ingress de Kubernetes vanilla — **usar en K8s sin OpenShift**, en vez de `route.yaml` (no ambos) |
| `configmap.yaml` | Variables de entorno NO sensibles |
| `secret.example.yaml` | Plantilla de las claves de Secret requeridas — **sin valores reales** |
| `hpa.yaml` | `HorizontalPodAutoscaler` opcional (autoescalado), no aplicado por defecto |
| `kustomization.yaml` | Amarra configmap + deployment + service + route para `kubectl apply -k` / `oc apply -k` |

## UIDs arbitrarios de OpenShift — compatibilidad verificada

El Dockerfile fija un usuario no-root (`mcpcorp`, uid/gid 1000) para correr
bajo Docker/Swarm. **OpenShift, con su SCC `restricted` por defecto, no
permite que un pod fuerce ese UID** — exige que el proceso corra con un
UID tomado del rango asignado dinámicamente al namespace (algo como
`1000670000-1000679999`), y si el manifiesto pidiera `runAsUser: 1000`
explícitamente, la admisión del pod sería **rechazada** (a menos que
alguien conceda una SCC más permisiva, como `anyuid`).

`deployment.yaml` **no fija `runAsUser` ni `runAsGroup`** a propósito, por
eso: deja que la plataforma decida. La imagen es compatible con
cualquier UID sin ningún ajuste porque el proceso **nunca escribe al
filesystem en runtime** — es stateless, `PYTHONDONTWRITEBYTECODE=1` evita
escribir `.pyc`, y no hay directorio de trabajo que necesite ser
escribible por un usuario concreto. En Kubernetes vanilla (sin esa
restricción de OpenShift), el contenedor simplemente corre con el uid
1000 que ya trae el Dockerfile — mismo resultado que en Docker/Swarm.

Si en el futuro el proceso necesitara escribir algo a disco (no es el
caso hoy), habría que seguir la convención estándar de imágenes
compatibles con OpenShift: hacer esos directorios escribibles por el
grupo `0` (root group), ya que OpenShift siempre asigna GID `0` como
grupo suplementario sin importar qué UID arbitrario elija.

## Cómo se probaría en un cluster real (no ejecutado aquí)

```bash
# 1. Crear el Secret real (nunca commitear este comando con el valor real)
oc create secret generic mcp-corp-secrets \
  --from-literal=MCP_CORP_AUDIT_HMAC_SECRET="$(python -c 'import secrets; print(secrets.token_hex(32))')" \
  --from-literal=MCP_CORP_POSTGRES__DSN="postgresql://usuario:password@host:5432/db"

# 2. Aplicar el resto de los manifiestos (OpenShift: incluye route.yaml)
oc apply -k deploy/openshift/

# En Kubernetes vanilla (sin OpenShift), en vez del paso 2:
#   kubectl apply -f configmap.yaml -f deployment.yaml -f service.yaml
#   kubectl apply -f ingress.yaml

# 3. Verificar
oc get pods -l app=mcp-corp
oc logs -l app=mcp-corp -f
curl -sk https://TODO-mcp-corp.example.com/health
curl -sk https://TODO-mcp-corp.example.com/ready
curl -sk https://TODO-mcp-corp.example.com/diagnostics
```
