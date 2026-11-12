-- Database initialisation (PostgreSQL + TimescaleDB)
-- The application database and the simulator ground-truth database are separate
-- (FR-018): diagnostic code paths only ever receive a connection to `oilintel`.
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE DATABASE oilintel_truth;
