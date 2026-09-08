from src.discount import discount_rate, discounted_total


def test_discount_rates() -> None:
    assert discount_rate("standard") == 0.0
    assert discount_rate("silver") == 0.05
    assert discount_rate("gold") == 0.10
    assert discount_rate("unknown") == 0.0


def test_discounted_total() -> None:
    assert discounted_total(200.0, "gold") == 180.0
