from pathlib import Path

import pytest
from dotenv import load_dotenv
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

# Config reads the environment at import time, so load .env first.
load_dotenv(Path(__file__).resolve().parent.parent / "backend" / ".env")

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.security.permissions import sync_catalogue  # noqa: E402
from config import TestConfig  # noqa: E402


@pytest.fixture(scope="session")
def app():
    application = create_app(TestConfig)

    uri = application.config["SQLALCHEMY_DATABASE_URI"]
    if not (make_url(uri).database or "").endswith("_test"):
        raise RuntimeError(
            "Refusing to run tests: the database name must end in _test. "
            "Check TEST_DATABASE_URL."
        )

    with application.app_context():
        db.create_all()
        # A migrated database always holds the role catalogue (migration 0010);
        # without it every permission check would depend on test order.
        sync_catalogue()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def db_session(app):
    """A database session whose work is rolled back after each test."""
    connection = db.engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    yield session
    session.close()
    transaction.rollback()
    connection.close()

