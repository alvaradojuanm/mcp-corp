"""Integración del conector de Postgres contra una base real.

Requiere el Postgres de desarrollo levantado:

    docker compose -f docker-compose.dev.yml up -d

Si no hay un Postgres alcanzable en `MCP_CORP_TEST_POSTGRES_DSN` (o el DSN
por defecto de docker-compose.dev.yml), estos tests se saltan con un
mensaje explícito en vez de fallar: no son parte de la suite unitaria
rápida, son la validación de que la abstracción funciona contra algo real.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import psycopg
import pytest

from mcp_corp.connectors.postgres import PostgresConnector
from mcp_corp.connectors.resilience import ResilienceConfig, ResilientExecutor

DSN = os.environ.get(
    "MCP_CORP_TEST_POSTGRES_DSN",
    "postgresql://mcp_corp:mcp_corp@localhost:5432/mcp_corp",
)


async def _postgres_reachable() -> bool:
    try:
        async with await psycopg.AsyncConnection.connect(DSN, connect_timeout=2):
            return True
    except Exception:
        return False


@pytest.fixture
async def connector() -> AsyncIterator[PostgresConnector]:
    if not await _postgres_reachable():
        pytest.skip(
            "Postgres de desarrollo no alcanzable en "
            f"{DSN!r}; levántalo con `docker compose -f docker-compose.dev.yml up -d`."
        )

    conn = PostgresConnector(
        name="postgres",
        dsn=DSN,
        min_pool_size=1,
        max_pool_size=5,
        pool_open_timeout_seconds=10.0,
    )
    await conn.connect()
    try:
        yield conn
    finally:
        await conn.close()


async def test_connect_and_health(connector: PostgresConnector) -> None:
    assert await connector.health() is True


async def test_run_executes_real_parameterized_query(connector: PostgresConnector) -> None:
    async def query(conn: psycopg.AsyncConnection) -> list[tuple[str, float]]:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT name, balance FROM accounts WHERE name = %s",
                ("cuenta-demo-1",),
            )
            return await cur.fetchall()

    rows = await connector.run(query)
    assert rows == [("cuenta-demo-1", 1000.00)]


async def test_pool_stats_reports_usage(connector: PostgresConnector) -> None:
    async def noop(conn: psycopg.AsyncConnection) -> None:
        await conn.execute("SELECT 1")

    await connector.run(noop)
    stats = connector.pool_stats()
    assert "pool_size" in stats or "connections_num" in stats


async def test_wrapped_in_resilient_executor(connector: PostgresConnector) -> None:
    executor = ResilientExecutor(
        connector,
        ResilienceConfig(
            source_name="postgres",
            max_concurrency=5,
            acquire_timeout_seconds=2.0,
            operation_timeout_seconds=5.0,
            failure_threshold=5,
            reset_timeout_seconds=30.0,
        ),
    )

    async def query(conn: psycopg.AsyncConnection) -> int:
        async with conn.cursor() as cur:
            await cur.execute("SELECT count(*) FROM accounts")
            (count,) = await cur.fetchone()
            return count

    result = await executor.run(query)
    assert result == 2
    assert executor.snapshot()["circuit_state"] == "closed"


async def test_close_then_run_is_rejected() -> None:
    if not await _postgres_reachable():
        pytest.skip("Postgres de desarrollo no alcanzable.")

    conn = PostgresConnector(
        name="postgres",
        dsn=DSN,
        min_pool_size=1,
        max_pool_size=2,
        pool_open_timeout_seconds=10.0,
    )
    await conn.connect()
    await conn.close()

    assert await conn.health() is False
    with pytest.raises(RuntimeError):
        await conn.run(lambda c: c.execute("SELECT 1"))
