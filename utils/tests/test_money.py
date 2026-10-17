import pytest

from utils.money import apply_pct_off, fmt_cents, line_total, parse_dollars, pct_off, savings, sum_cents, to_dollars_str


def test_fmt():
    assert fmt_cents(1234) == "$12.34"
    assert fmt_cents(5) == "$0.05"
    assert fmt_cents(-150) == "-$1.50"
    assert fmt_cents(123456) == "$1,234.56"
    assert to_dollars_str(-5) == "-0.05"
    with pytest.raises(TypeError):
        fmt_cents(12.5)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value,cents",
    [
        ("$12.34", 1234),
        ("12", 1200),
        ("1,234.5", 123450),
        (" $0.995 ", 100),
        (40, 4000),
        (12.34, 1234),
        ("(2.50)", -250),
    ],
)
def test_parse(value, cents):
    assert parse_dollars(value) == cents


def test_parse_bad():
    with pytest.raises(ValueError):
        parse_dollars("forty")


def test_math():
    assert sum_cents([100, 250, 1]) == 351
    assert line_total(299, 3) == 897
    assert apply_pct_off(1000, 25) == 750
    assert apply_pct_off(999, 50) == 499  # 499.5 off rounds to 500
    assert pct_off(1000, 750) == 25
    assert pct_off(0, 0) == 0
    assert savings(500, 600) == 0
    with pytest.raises(TypeError):
        sum_cents([1, 2.0])  # type: ignore[list-item]
    with pytest.raises(ValueError):
        apply_pct_off(100, 120)
