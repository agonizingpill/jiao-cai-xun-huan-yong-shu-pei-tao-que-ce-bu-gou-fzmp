import pytest
from helpers import engine
from src.db import Base

@pytest.fixture(autouse=True)
def _schema():
    Base.metadata.create_all(engine)
    yield
