-- Datos semilla mínimos para el Postgres de desarrollo (docker-compose.dev.yml).
-- Solo para probar el conector de la Fase 2 contra una base real; no es
-- esquema de negocio definitivo (eso llega con las tools de fases futuras).

CREATE TABLE IF NOT EXISTS accounts (
    id      SERIAL PRIMARY KEY,
    name    TEXT NOT NULL,
    balance NUMERIC(14, 2) NOT NULL
);

INSERT INTO accounts (name, balance) VALUES
    ('cuenta-demo-1', 1000.00),
    ('cuenta-demo-2', 2500.50);

-- Fase 3/4: clientes consultados por la tool `consultar_cliente`. La
-- columna `cedula` guarda la forma canónica CORTA que produce
-- `identifiers.normalizar(...).cedula` (prefijo + 8 dígitos, sin dígito
-- verificador) — así, sin importar en qué formato haya escrito el
-- identificador quien invoca la tool ("V16760320", "V-16.760.320",
-- "16760320"...), la consulta a Postgres siempre busca la misma forma.
--
-- V16760320 y V16760321 también existen en el stub de saldos
-- (deploy/dev/saldo_api_stub.py) para que `resumen_cliente` cuadre en el
-- caso feliz; V16760322 existe SOLO aquí (no en el stub) a propósito,
-- para poder probar el caso de resultado parcial (cliente sí, saldo no).
CREATE TABLE IF NOT EXISTS clientes (
    id     SERIAL PRIMARY KEY,
    cedula TEXT UNIQUE NOT NULL,
    nombre TEXT NOT NULL,
    email  TEXT NOT NULL,
    estado TEXT NOT NULL DEFAULT 'activo'
);

INSERT INTO clientes (cedula, nombre, email, estado) VALUES
    ('V16760320', 'Ana María Restrepo', 'ana.restrepo@example.com', 'activo'),
    ('V16760321', 'Carlos Andrés Gómez', 'carlos.gomez@example.com', 'moroso'),
    ('V16760322', 'Lucía Fernanda Ríos', 'lucia.rios@example.com', 'activo'),
    ('V90000001', 'Cliente Error Simulado', 'error.simulado@example.com', 'activo');
