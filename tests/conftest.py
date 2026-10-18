import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_ENABLED", "false")
os.environ.setdefault("LLM_PROVIDER", "mock")
os.environ.setdefault("LOG_LEVEL", "ERROR")
os.environ.setdefault("AUTO_CREATE_TABLES", "true")

import pytest

from app.db import Base, engine
from app.services.workflows import seed_builtin_workflows


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def workflows():
    from app.db import SessionLocal

    with SessionLocal() as session:
        return {d.name: d for d in seed_builtin_workflows(session)}


@pytest.fixture
def session():
    from app.db import SessionLocal

    with SessionLocal() as session:
        yield session
