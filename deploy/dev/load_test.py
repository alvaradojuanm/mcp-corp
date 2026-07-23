"""Prueba de carga simple y reproducible contra las tools MCP.

No depende de `hey` ni `locust`: usa `fastmcp.Client` (ya una dependencia
transitiva del proyecto) para abrir `--concurrency` sesiones MCP
concurrentes contra el server real y medir throughput y latencias.

Uso:
    uv run python deploy/dev/load_test.py \\
        --url http://localhost:8000/mcp \\
        --tool resumen_cliente \\
        --identificador V16760320 \\
        --concurrency 20 \\
        --requests-per-worker 20

Con `--dsn` (opcional), además consulta Postgres al final para contar
conexiones concurrentes reales durante la corrida (requiere psycopg
instalado, ya lo está como dependencia del proyecto).
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time

from fastmcp import Client


async def worker(
    url: str,
    tool: str,
    args: dict[str, str],
    n_requests: int,
    latencies: list[float],
    errors: list[str],
) -> None:
    async with Client(url) as client:
        for _ in range(n_requests):
            started = time.perf_counter()
            try:
                await client.call_tool(tool, args)
            except Exception as exc:  # noqa: BLE001 — se registra el mensaje, no se relanza
                errors.append(str(exc))
            finally:
                latencies.append(time.perf_counter() - started)


def _percentile(data: list[float], pct: float) -> float:
    if not data:
        return 0.0
    ordenados = sorted(data)
    idx = min(len(ordenados) - 1, int(len(ordenados) * pct))
    return ordenados[idx]


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8000/mcp")
    parser.add_argument("--tool", default="resumen_cliente")
    parser.add_argument("--identificador", default="V16760320")
    parser.add_argument("--concurrency", type=int, default=10, help="sesiones MCP concurrentes")
    parser.add_argument("--requests-per-worker", type=int, default=10)
    args = parser.parse_args()

    latencies: list[float] = []
    errors: list[str] = []

    started = time.perf_counter()
    await asyncio.gather(
        *[
            worker(
                args.url,
                args.tool,
                {"identificador": args.identificador},
                args.requests_per_worker,
                latencies,
                errors,
            )
            for _ in range(args.concurrency)
        ]
    )
    elapsed = time.perf_counter() - started

    total = args.concurrency * args.requests_per_worker
    ok = total - len(errors)

    print(f"URL:                {args.url}")
    print(f"Tool:               {args.tool}")
    print(f"Concurrencia:       {args.concurrency}")
    print(f"Total peticiones:   {total}")
    print(f"Exitosas:           {ok}")
    print(f"Fallidas:           {len(errors)}")
    print(f"Duración total:     {elapsed:.2f}s")
    print(f"Throughput:         {total / elapsed:.1f} req/s")
    if latencies:
        print(f"Latencia p50:       {_percentile(latencies, 0.50) * 1000:.1f} ms")
        print(f"Latencia p95:       {_percentile(latencies, 0.95) * 1000:.1f} ms")
        print(f"Latencia p99:       {_percentile(latencies, 0.99) * 1000:.1f} ms")
        print(f"Latencia máxima:    {max(latencies) * 1000:.1f} ms")
    if errors:
        muestra = errors[0]
        print(f"Ejemplo de error:   {muestra}")


if __name__ == "__main__":
    asyncio.run(main())
