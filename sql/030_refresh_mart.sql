-- Execute through psycopg with {'as_of': datetime.date(...)} in the ETL transaction.
-- Standalone psql equivalent:
-- SELECT mart.refresh_market_corridor(DATE '2026-10-26');
SELECT mart.refresh_market_corridor(%(as_of)s::date) AS mart_rows;
