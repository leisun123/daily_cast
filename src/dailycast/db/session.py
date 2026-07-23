"""SQLite engine and session factory construction."""

from collections.abc import Generator

from sqlalchemy import Engine, event
from sqlalchemy.engine import create_engine
from sqlalchemy.orm import Session, sessionmaker

from dailycast.core.config import DatabaseSettings


def create_sqlite_engine(settings: DatabaseSettings) -> Engine:
    """Create the single-process SQLite engine with the approved local pragmas."""
    engine = create_engine(
        settings.url,
        connect_args={"check_same_thread": False},
        echo=settings.echo,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def configure_sqlite_connection(dbapi_connection: object, connection_record: object) -> None:
        """Enable per-connection SQLite safety settings without changing journal mode."""
        del connection_record
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("PRAGMA busy_timeout = 5000")
        finally:
            cursor.close()

    # WAL is a database-wide mode, not a per-connection setting. Reasserting it in the
    # connection hook can block a new synchronous connection while a writer is active.
    # Configure it once while building the application's single SQLite engine instead.
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode = WAL")

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return the application session factory; it does not create schema."""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def session_scope(factory: sessionmaker[Session]) -> Generator[Session, None, None]:
    """Yield a session and always close it after the caller finishes."""
    session = factory()
    try:
        yield session
    finally:
        session.close()
