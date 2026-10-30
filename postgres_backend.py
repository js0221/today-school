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
                connect_timeout=15, sslmode='require')
        except psycopg.Error as error:
            # Never print the raw exception: drivers can include connection secrets.
            message = str(error).lower()
            if 'password authentication failed' in message or getattr(error, 'sqlstate', None) == '28P01':
                reason = 'DB_AUTH_FAILED: check current Neon password in DATABASE_URL'
            elif 'unsupported startup parameter' in message:
                reason = 'DB_POOLER_OPTION: unsupported connection startup option'
            elif 'translate host name' in message or 'name or service not known' in message or 'getaddrinfo' in message:
                reason = 'DB_DNS_FAILED: check Neon hostname'
            elif 'timeout' in message or 'timed out' in message:
                reason = 'DB_TIMEOUT: Neon did not respond within the connection timeout'
            elif 'ssl' in message or 'certificate' in message or 'channel binding' in message:
                reason = 'DB_TLS_FAILED: check TLS and channel binding settings'
            elif 'invalid' in message or 'missing' in message or 'uri' in message:
                reason = 'DB_URL_INVALID: check full DATABASE_URL format'
            else:
                reason = 'DB_CONNECT_FAILED: check Neon endpoint availability and connection settings'
            raise sqlite3.OperationalError(reason) from None

    def execute(self, statement, parameters=()):
        try:
            # Transaction-local settings work with Neon transaction pooling.
            self.connection.execute("SET LOCAL statement_timeout = '10s'")
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
