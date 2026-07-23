# Fase 03 — Conector HTTP y tools MCP

| Campo | Valor |
|---|---|
| **Fase** | 03 |
| **Objetivo** | Conector HTTP + 3 tools por intención de negocio + Resource + Prompt + auditoría por invocación |
| **Repo** | `github.com/alvaradojuanm/mcp-corp` |
| **Commits** | `0713523` → `bf0b658` (9 commits) + `ec2a694`, `ffe2a96` (corrección HMAC) |
| **Estado** | ✅ **CERRADA** |
| **Fecha de cierre** | 23 de julio de 2026 |
| **Versiones pineadas** | `httpx==0.28.1` (pasa de transitiva a directa) |

---

## Resultado

**El patrón heterogéneo quedó demostrado y el camino completo cerrado:** agente → tool → conector → dato, con dos fuentes de naturaleza distinta (driver nativo de Postgres y HTTP) detrás de tools homogéneas. 44/44 tests pasando contra stack real, validado con un cliente MCP.

### Estructura resultante

```
src/mcp_corp/
├── audit.py                  # auditoría por invocación (nuevo)
├── tools.py                  # 3 tools + Resource + Prompt (nuevo)
└── connectors/http.py        # conector HTTP (nuevo)

tests/
├── test_audit.py
├── connectors/test_http.py
└── tools/
    ├── test_tools_logic.py
    └── test_tools_integration.py

deploy/dev/saldo_api_stub.py  # stub determinista, stdlib puro
```

**Modificados:** `config.py`, `server.py`, `main.py`, `registry.py`, `logging_setup.py`, `docker-compose.dev.yml`, `postgres-seed.sql`, `.env.example`, `README.md`.

### Las tres tools

| Tool | Fuente | Nota |
|---|---|---|
| `consultar_cliente(cedula)` | PostgreSQL | driver nativo, SQL parametrizado |
| `consultar_saldo(cedula)` | API REST (stub) | HTTP |
| `resumen_cliente(cedula)` | ambas | paralelo con `TaskGroup`, resultado parcial explícito |

Parámetro compartido: `cedula: Annotated[str, Field(pattern=r"^\d{6,10}$")]`.

**Desambiguación cruzada en las descripciones** — cada tool le indica al modelo cuándo NO usarla y cuál usar en su lugar ("úsala cuando necesites SOLO los datos del cliente; si también necesitas el saldo, usa `resumen_cliente`"). Este es el punto donde la mayoría de servers MCP falla: descripciones que un dev entiende pero que dejan al modelo dudando entre tools parecidas.

---

## Verificaciones de aceptación

- [x] **44/44 tests pasando** contra Postgres real + stub de API
- [x] 10 del conector HTTP (timeout, breaker por fallo de transporte y por 5xx, semáforo)
- [x] 5 integración de tools contra stack real; 11 de lógica de tools
- [x] 4 de auditoría, incluida la verificación del HMAC
- [x] Test de **fuente caída**: la tool compuesta devuelve parcial marcado, no falla entera
- [x] Test de **no-filtración de internals** hacia el modelo
- [x] Resource y Prompt implementados y registrados
- [x] Auditoría por invocación con correlation id, enmascaramiento HMAC
- [x] `pip-audit`: 82 paquetes, **`No known vulnerabilities found`**
- [x] Autoría verificada: solo `alvaradojuanm` + noreply
- [x] Validado end-to-end con un cliente MCP real

---

## Hallazgo crítico: el `JSONFormatter` descartaba `extra={}` en silencio

**Desde la Fase 1.** El formateador de logs ignoraba todos los campos pasados vía `extra={}`, lo que significa que el correlation id que dimos por bueno en la Fase 1 y los eventos del circuit breaker de la Fase 2 **nunca se escribieron**. Ambas fases pasaron su verificación con esto roto.

El agente lo encontró al implementar la auditoría —la primera funcionalidad que de verdad consumía ese mecanismo— y lo corrigió verificando antes/después.

> **Lección: lo que se cablea pero no se usa, no se valida.** El correlation id estuvo dos fases muerto porque ninguna funcionalidad lo consumía todavía. Cuando una fase deja infraestructura "preparada para el futuro", esa preparación no está probada hasta que algo la use — hay que tratarla como no verificada, no como hecha.

---

## Corrección de seguridad: SHA256 plano → HMAC-SHA256

**El problema.** La primera implementación enmascaraba la cédula con `sha256:<12 hex>`, descrito como "correlacionable, no reversible". **Era incorrecto.** Una cédula es un identificador de espacio pequeño y enumerable: con el patrón `^\d{6,10}$`, cualquiera con acceso al log puede generar la tabla completa de hashes posibles en segundos y revertir cada valor. SHA256 protege secretos de alta entropía, no identificadores numéricos cortos.

Es decir: el log de auditoría contenía cédulas de clientes en un formato que *parecía* protegido y no lo estaba.

**El arreglo.** `hmac.new(secret, valor, "sha256")` en vez de `hashlib.sha256(valor)`. Sigue siendo determinista (misma cédula → mismo valor, correlacionable igual), pero sin la clave no se puede construir la tabla.

- Nueva variable `MCP_CORP_AUDIT_HMAC_SECRET`, vacía por defecto, nunca un valor real en el repo.
- **La clave debe ser idéntica en todas las réplicas** — si no, la misma cédula produce valores distintos según qué réplica atendió y la correlación se rompe. Documentado.
- Formato del log: `hmac-sha256:...`.
- Rotar la clave **rompe la correlación histórica a propósito** — es el comportamiento deseado ante sospecha de compromiso. Documentado en el README junto con el ataque que motivó el cambio.
- Tests: `HMAC(clave_A) ≠ HMAC(clave_B)` para el mismo valor, ya no coincide con SHA256 plano, y la correlación se mantiene con la misma clave.

### ⚠️ Pendiente fichado: el arranque sin clave es blando

Si `MCP_CORP_AUDIT_HMAC_SECRET` viene vacía, el server registra un warning y arranca igual. **HMAC con clave vacía es determinista y públicamente reproducible** — vuelve exactamente al problema de la tabla arcoíris.

Aceptable para desarrollo. Cuando exista el concepto de "modo producción", esto debe ser **fail-closed**: no arrancar sin clave.

---

## Decisiones tomadas por el agente

| Decisión | Veredicto |
|---|---|
| Quitar `from __future__ import annotations` de `tools.py` | ✅ Necesaria — rompía la reconstrucción del `TypeAdapter` de Pydantic al registrar el Prompt |
| Errores de negocio cuentan como éxito para el breaker | ✅ Coherente con el criterio aprobado en Fase 2 |
| Medio-abierto reabre al primer fallo | ✅ Coherente con Fase 2 |
| `httpx` pasa de transitiva a dependencia directa | ✅ Correcto — se usa en runtime, debe declararse |

---

## Validación del diseño de la Fase 2

**El conector HTTP no resultó forzado — salió más simple que Postgres.** No hay pool que administrar (`httpx.AsyncClient` trae el suyo), y **la abstracción de la Fase 2 no necesitó ningún cambio**.

Esto valida el diseño de dos capas con evidencia real: la capa de resiliencia genérica ya tiene dos inquilinos de naturaleza distinta y ninguno la deformó. Agregar una tercera fuente debería ser igual de directo.

---

## Pendientes

- [ ] **Formato de cédula.** El patrón `^\d{6,10}$` acepta solo dígitos, pero en Venezuela se escriben `V-12345678` / `E-12345678`. Decidir conscientemente: ¿la tool acepta el formato completo, o el modelo debe pasar solo dígitos? Si un usuario le dice al agente "cédula V-12345678", el modelo tendría que saber que debe quitar la letra.
- [ ] **Fail-closed sin clave HMAC** cuando exista modo producción.
- [ ] Evaluar si el fix de Windows (`WindowsSelectorEventLoopPolicy`) debe salir de `main.py` (heredado de Fase 2).
- [ ] Probar contra la instancia de Postgres 18 existente en `localhost:5433` (heredado de Fase 2).

---

## Lecciones

1. **Lo que se cablea pero no se usa, no se valida.** El bug del `JSONFormatter` sobrevivió dos fases porque nada consumía el mecanismo todavía.
2. **"Hasheado" no es "protegido".** Para identificadores de espacio pequeño, un hash plano es reversible por enumeración. HMAC con clave secreta es el mínimo.
3. **Las descripciones de tools son la interfaz real del server** y las lee un modelo, no un humano. La desambiguación cruzada entre tools parecidas es donde se gana o se pierde la calidad de las decisiones del agente.
4. **La abstracción se valida con el segundo inquilino, no con el primero.** Hasta la Fase 3, el diseño de conectores era una hipótesis.

---

## Siguiente

**Fase 04 — Despliegue y escalado:** compose de réplicas, prueba de escalado horizontal en Swarm/Portainer, y verificación de la fórmula de capacidad (`límite por réplica = techo de la fuente ÷ nº de réplicas`) bajo carga real.