"""Configuración compartida de pytest.

En Windows, el event loop por defecto de asyncio es `ProactorEventLoop`,
que psycopg3 no soporta en modo async (ver `mcp_corp.main` para el mismo
ajuste en el entrypoint del proceso). Sin esto, cualquier test que abra una
conexión real a Postgres falla con `InterfaceError` antes de llegar a
tocar la base.
"""

from __future__ import annotations

import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
