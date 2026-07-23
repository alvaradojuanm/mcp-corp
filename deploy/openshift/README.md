# Manifiestos de OpenShift / Kubernetes

Esqueleto de despliegue para el destino final del proyecto (ver README
raíz, sección "Fase 1"). **No se probó contra un cluster real** — este
entorno no tenía uno disponible — pero la estructura, las sondas y las
variables de entorno son las mismas que ya se validaron corriendo el
server directamente y en Docker Swarm.

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
| `deployment.yaml` | Deployment: réplicas, sondas, recursos, variables de entorno |
| `service.yaml` | Service ClusterIP: expone el puerto 8000 dentro del cluster |
| `route.yaml` | Route de OpenShift (equivalente a un Ingress en K8s vanilla) |
| `configmap.yaml` | Variables de entorno NO sensibles |
| `secret.example.yaml` | Plantilla de las claves de Secret requeridas — **sin valores reales** |
| `kustomization.yaml` | Amarra todo lo anterior para `kubectl apply -k` / `oc apply -k` |

## Cómo se probaría en un cluster real (no ejecutado aquí)

```bash
# 1. Crear el Secret real (nunca commitear este comando con el valor real)
oc create secret generic mcp-corp-secrets \
  --from-literal=MCP_CORP_AUDIT_HMAC_SECRET="$(python -c 'import secrets; print(secrets.token_hex(32))')" \
  --from-literal=MCP_CORP_POSTGRES__DSN="postgresql://usuario:password@host:5432/db"

# 2. Aplicar el resto de los manifiestos
oc apply -k deploy/openshift/

# 3. Verificar
oc get pods -l app=mcp-corp
oc logs -l app=mcp-corp -f
```
