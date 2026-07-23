"""Stub determinista de una API REST de saldos.

Se levanta en `docker-compose.dev.yml` para probar el conector HTTP de la
Fase 3 contra algo real (no solo mocks): sin dependencias externas, usa
únicamente la librería estándar de Python.

Endpoints:
  GET /health              -> 200 {"status": "ok"}
  GET /saldos/{cedula}     -> 200 {"cedula", "saldo", "moneda"} si existe
                               404 {"error": "cedula_no_encontrada"} si no
                               500 {"error": "error_simulado"} para la
                               cédula reservada `CEDULA_ERROR_SIMULADO`,
                               para poder probar el circuit breaker contra
                               un fallo real de infraestructura.

Las cédulas V16760320 y V16760321 coinciden con las del seed de Postgres
(`deploy/dev/postgres-seed.sql`) a propósito, para que `resumen_cliente`
cuadre en el caso feliz. Los identificadores que llegan aquí ya vienen en
forma canónica corta (prefijo + 8 dígitos, sin dígito verificador) porque
`tools.py` normaliza antes de invocar el conector — este stub no sabe nada
de formatos con puntos, guiones ni dígito verificador.
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SALDOS: dict[str, dict[str, object]] = {
    "V16760320": {"cedula": "V16760320", "saldo": 1500000.50, "moneda": "COP"},
    "V16760321": {"cedula": "V16760321", "saldo": 320000.00, "moneda": "COP"},
}

CEDULA_ERROR_SIMULADO = "V90000001"

SALDO_PATH = re.compile(r"^/saldos/(?P<cedula>[A-Z0-9]+)$")


class Handler(BaseHTTPRequestHandler):
    def _write_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 — nombre exigido por BaseHTTPRequestHandler
        if self.path == "/health":
            self._write_json(200, {"status": "ok"})
            return

        match = SALDO_PATH.match(self.path)
        if match:
            cedula = match.group("cedula")
            if cedula == CEDULA_ERROR_SIMULADO:
                self._write_json(500, {"error": "error_simulado"})
                return
            saldo = SALDOS.get(cedula)
            if saldo is None:
                self._write_json(404, {"error": "cedula_no_encontrada"})
                return
            self._write_json(200, saldo)
            return

        self._write_json(404, {"error": "ruta_no_encontrada"})

    def log_message(self, format: str, *args: object) -> None:
        pass  # silencia el log de acceso por request de BaseHTTPRequestHandler


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)
    server.serve_forever()
