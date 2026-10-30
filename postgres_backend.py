"""PostgreSQL adapter for the application's parameterized SQL statements."""
import sqlite3


def translate(statement):
    if statement.strip().upper() == 'BEGIN IMMEDIATE':
        # Serialize write groups, including login limits, across server threads.
        return 'SELECT pg_advisory_xact_lock(73102616)'
    statement = statement.replace('INTEGER PRIMARY KEY', 'SERIAL PRIMARY KEY')
    statement = statement.replace(' REAL ', ' DOUBLE PRECISION ')
    statement = statement.replace('MIN(expires,?)', 'LEAST(expires,?)')
    if statement.strip() == 'INSERT OR REPLACE INTO limits VALUES (?,?,1)':
        statement = ('INSERT INTO limits(key,started,count) VALUES (?,?,1) '
                     'ON CONFLICT(key) DO UPDATE SET started=excluded.started,count=1')
    return statement.replace('?', '%s')


class PostgresConnection:
    def __init__(self, url):
        import psycopg
        from psycopg.rows import dict_row
        self.driver = psycopg
        try:
            self.connection = psycopg.connect(url, row_factory=dict_row,
                connect_timeout=10, sslmode='require', options='-c statement_timeout=10000')
        except psycopg.Error:
            raise sqlite3.OperationalError('External database connection failed') from None

    def execute(self, statement, parameters=()):
        try:
            return self.connection.execute(translate(statement), parameters or None)
        except self.driver.IntegrityError:
            raise sqlite3.IntegrityError('Database constraint failed') from None
        except self.driver.Error:
            raise sqlite3.DatabaseError('External database query failed') from None

    def executescript(self, script):
        for statement in script.split(';'):
            if statement.strip():
                self.execute(statement)

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, error_type, error, traceback):
        try:
            if error_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            self.close()
