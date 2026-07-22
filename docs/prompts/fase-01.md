# Fase 01 — Andamiaje base

> Bitácora de trazabilidad: copia exacta del prompt que originó esta fase.

Eres mi copiloto de ingeniería en un proyecto nuevo. Vamos a construir la IMPLEMENTACIÓN DE REFERENCIA de un server MCP corporativo en Python. Este repo será la plantilla que luego clonaremos para múltiples fuentes de datos, así que la calidad y la claridad importan más que la velocidad. Trabajaremos POR FASES; esta es la FASE 1 y su único objetivo es el andamiaje base. NO implementes tools, conectores a datos, ni lógica de negocio todavía.

Contexto y norte (para que entiendas las decisiones, no para implementarlo ahora)

* El server usará FastMCP (paquete standalone `fastmcp`, versión 3.x) sobre transporte Streamable HTTP.
* Debe ser STATELESS por invocación, para escalar horizontalmente por réplicas detrás de un balanceador (hoy Docker Swarm/Traefik en Portainer; destino final OpenShift/Kubernetes).
* Diseño 12-factor: config por variables de entorno, logs a stdout, sin estado local en disco, apagado graceful ante SIGTERM.
* Más adelante (otras fases) añadiremos: una capa de conectores donde cada fuente tiene su límite de concurrencia, timeout y circuit breaker; tools por intención de negocio; y un gateway de gobierno por encima. NADA de eso va en esta fase.

IMPORTANTE antes de escribir código
La versión 3.0 de FastMCP fue una reescritura grande; tu conocimiento puede estar desactualizado. CONSULTA la documentación actual de FastMCP 3.x para confirmar la API correcta de: (1) creación del server, (2) ejecución con transporte Streamable HTTP, (3) cómo añadir rutas HTTP personalizadas (las necesito para health/readiness), y (4) hooks de ciclo de vida/lifespan para el shutdown. Si algo no lo puedes verificar, dímelo en vez de inventar.
Alcance EXACTO de la Fase 1
Crea esta estructura y nada más:
mcp-corp/ ├── pyproject.toml # deps con uv, Python 3.12+, fastmcp 3.x PINEADO a versión exacta ├── .python-version ├── .gitignore # DEBE excluir .env, secrets, pycache, .venv, *.log, etc. ├── .dockerignore ├── .env.example # todas las variables, con valores de ejemplo (NUNCA un .env real) ├── README.md # explica el proyecto, cómo levantarlo y cada decisión de diseño ├── Dockerfile # imagen slim, deps pineadas, usuario NO-root, healthcheck ├── docs/ │ └── prompts/ │ └── fase-01.md # este mismo prompt, guardado como bitácora (ver sección Git) ├── src/ │ └── mcp_corp/ │ ├── init.py │ ├── main.py # entrypoint (python -m mcp_corp) │ ├── server.py # bootstrap del FastMCP server + registro de rutas + lifecycle │ ├── config.py # pydantic-settings: toda la config desde entorno │ ├── logging_setup.py # logging estructurado en JSON a stdout │ └── connectors/ │ └── init.py # vacío por ahora (placeholder para la Fase 2) ├── tests/ │ └── init.py └── deploy/ ├── swarm/ │ └── docker-compose.yml # servicio con deploy.replicas, healthcheck, labels de Traefik └── openshift/ └── .gitkeep # placeholder; los manifiestos K8s vienen en otra fase
Requisitos NO NEGOCIABLES de cada pieza

* config.py: usa pydantic-settings. Toda config (host, puerto, nivel de log, nombre del servicio, tamaño de pool por defecto, etc.) viene de variables de entorno con defaults sensatos. Nada hardcodeado. Refleja cada variable en .env.example.
* logging_setup.py: logs estructurados en JSON a stdout. Deja preparado un mecanismo de "correlation id" por request (aunque aún no haya tools, la infraestructura de logging de auditoría debe existir desde ya). Nivel configurable por entorno.
* server.py: crea el FastMCP server sobre Streamable HTTP. Expón DOS rutas HTTP separadas: `/health` (liveness: el proceso vive) y `/ready` (readiness: listo para recibir tráfico). Deben ser endpoints distintos, porque Swarm usa el healthcheck y OpenShift necesita liveness y readiness probes por separado. Implementa apagado graceful ante SIGTERM (cerrar limpio).
* Dockerfile: base slim, dependencias pineadas, corre como usuario NO-root (requisito de seguridad para banca), incluye HEALTHCHECK apuntando a /health.
* deploy/swarm/docker-compose.yml: define el servicio con `deploy: replicas: 2` (para demostrar escalado horizontal), healthcheck, y labels de Traefik para balancear entre réplicas. Comenta el archivo para que se entienda.
* README.md: debe explicar qué es el proyecto, cómo levantarlo con uv en local, cómo correrlo en Docker, cómo probar /health y /ready, y una sección "Decisiones de diseño" que documente por qué stateless, por qué health+readiness separados, por qué usuario no-root, etc. Este README es parte del entregable, no un extra.

Git

* Configura la identidad de Git para este repo ANTES de cualquier commit: git config user.name "alvaradojuanm" git config user.email "<correo asociado a la cuenta de GitHub alvaradojuanm>" (Si no lo tienes definido, avísame y lo confirmo antes de que commitees. Puede ser el correo no-reply de GitHub del tipo ID+alvaradojuanm@users.noreply.github.com para no exponer el real.)
* POLÍTICA DE AUTORÍA (OBLIGATORIA E INNEGOCIABLE): todos los commits deben aparecer ÚNICAMENTE bajo el usuario alvaradojuanm. NO añadas líneas "Co-authored-by:". NO añadas firmas de herramienta (nada de "Generated with...", "🤖", ni similares). NO te acredites como coautor de ninguna forma. El mensaje de commit debe contener SOLO la descripción del cambio, nada más.
* Inicializa el repo con rama por defecto `main`.
* Primer commit en `main`: solo el bootstrap mínimo (README, .gitignore, LICENSE si aplica).
* Crea y cámbiate a la rama `feat/estructura-base` y construye ahí todo el andamiaje.
* Añade el remoto: git@github.com:alvaradojuanm/mcp-corp.git
* Guarda una copia EXACTA de este prompt en el repo como docs/prompts/fase-01.md, e inclúyelo en los commits de esta fase (es nuestra bitácora de trazabilidad; cada fase tendrá su archivo).
* Haz commits atómicos y descriptivos. Empuja la rama `feat/estructura-base` al remoto.
* NUNCA incluyas .env, credenciales ni secrets en ningún commit. Verifica el .gitignore antes de commitear.

Al terminar

1. Verifica que el proyecto levanta con uv en local y que /health y /ready responden.
2. Verifica que el contenedor construye y corre.
3. Verifica que el server MCP es alcanzable con el MCP Inspector.
4. Verifica con `git log` que los commits salen SOLO a nombre de alvaradojuanm, sin coautorías ni firmas de herramienta. Si aparece cualquier otra autoría, corrígelo antes de empujar.
5. Dame un resumen de: qué versión exacta de fastmcp pineaste, qué archivos creaste, qué comandos usar para levantarlo, y cualquier decisión donde tuviste que elegir por mí.
6. NO avances a tools ni conectores. Esta fase termina en el andamiaje.

Si algo es ambiguo o no puedes verificar contra la documentación actual, PREGUNTA antes de asumir. Prefiero corregir el rumbo ahora que rehacer después.
