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

-- Fase 3: clientes consultados por la tool `consultar_cliente`. Las cédulas
-- 1000000001 y 1000000002 también existen en el stub de saldos
-- (deploy/dev/saldo_api_stub.py) para que `resumen_cliente` cuadre en el
-- caso feliz; 1000000003 existe SOLO aquí (no en el stub) a propósito,
-- para poder probar el caso de resultado parcial (cliente sí, saldo no).
CREATE TABLE IF NOT EXISTS clientes (
    id     SERIAL PRIMARY KEY,
    cedula TEXT UNIQUE NOT NULL,
    nombre TEXT NOT NULL,
    email  TEXT NOT NULL,
    estado TEXT NOT NULL DEFAULT 'activo'
);

INSERT INTO clientes (cedula, nombre, email, estado) VALUES
    ('1000000001', 'Ana María Restrepo', 'ana.restrepo@example.com', 'activo'),
    ('1000000002', 'Carlos Andrés Gómez', 'carlos.gomez@example.com', 'moroso'),
    ('1000000003', 'Lucía Fernanda Ríos', 'lucia.rios@example.com', 'activo');
