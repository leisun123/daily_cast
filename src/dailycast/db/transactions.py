"""Transactional session boundary for DailyCast application services."""

from types import TracebackType
from typing import Literal

from sqlalchemy.orm import Session, sessionmaker


class UnitOfWork:
    """Commit on successful scope completion and roll back on an exception."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self.session: Session | None = None

    def __enter__(self) -> "UnitOfWork":
        self.session = self._session_factory()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """Commit the unit or undo all uncommitted database mutations."""
        del exc_value, traceback
        if self.session is None:
            return False
        try:
            if exc_type is None:
                self.session.commit()
            else:
                self.session.rollback()
        finally:
            self.session.close()
            self.session = None
        return False
