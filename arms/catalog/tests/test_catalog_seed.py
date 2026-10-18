"""Seed renderer: SQL generated from data files (tests/data fixture), literal quoting, URL handling."""

from datetime import UTC, date, datetime
from pathlib import Path

from arms.catalog.seed import lit, main, psycopg_url, render_seed

TEST_DATA = Path(__file__).parent / "data"


def test_literals():
    assert lit(None) == "NULL"
    assert lit(True) == "TRUE"
    assert lit(42) == "42"
    assert lit("O'Hara") == "'O''Hara'"
    assert lit([]) == "'{}'::text[]"
    assert lit(["a", "b'c"]) == "ARRAY['a', 'b''c']::text[]"
    assert lit({"spicy": 0.5}) == """'{"spicy": 0.5}'::jsonb"""
    assert lit(date(2026, 10, 18)) == "'2026-10-18'::date"
    assert lit(datetime(2026, 10, 18, tzinfo=UTC)).endswith("::timestamptz")


def test_psycopg_url():
    assert psycopg_url("postgresql+psycopg://picky:picky@localhost:5432/picky") == (
        "postgresql://picky:picky@localhost:5432/picky"
    )
    assert psycopg_url("postgresql://x@h/db") == "postgresql://x@h/db"


def test_render_seed_covers_every_data_file():
    sql = render_seed(TEST_DATA)
    assert sql.count("INSERT INTO products ") == 13
    assert sql.count("INSERT INTO slots ") == 14 and sql.count("INSERT INTO inventory ") == 14
    assert sql.count("INSERT INTO stores ") == 3
    assert "'store_ave:D1-1-0'" in sql and "'online:salsa_hot'" in sql  # deal ids
    assert sql.count("INSERT INTO lots ") == 5
    assert "INSERT INTO users " in sql and "INSERT INTO mandates " in sql and "INSERT INTO feedback " in sql
    assert sql.strip().startswith("--") and "BEGIN;" in sql and sql.rstrip().endswith("COMMIT;")
    assert "CREATE TABLE" not in sql
    assert "CREATE TABLE" in render_seed(TEST_DATA, include_schema=True)
    assert "INSERT INTO users " not in render_seed(TEST_DATA, include_users=False)


def test_cli_out_file(tmp_path, capsys):
    out = tmp_path / "seed.sql"
    assert main(["--data-dir", str(TEST_DATA), "--out", str(out)]) == 0
    assert "INSERT INTO products" in out.read_text()
    assert main(["--data-dir", str(tmp_path / "missing")]) == 1
