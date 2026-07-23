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
