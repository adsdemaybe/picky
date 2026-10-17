import importlib.util

import pytest

from utils import db


def test_engine_is_lazy_and_shared() -> None:
    if importlib.util.find_spec("sqlalchemy") is None:
        with pytest.raises(db.DatabaseUnavailable, match="extra catalog"):
            db.get_engine("postgresql+psycopg://u:p@localhost:5432/x")
        return
    pytest.importorskip("psycopg")
    url = "postgresql+psycopg://u:p@localhost:5432/x"  # no connection is made until the first query
    assert db.get_engine(url) is db.get_engine(url)
