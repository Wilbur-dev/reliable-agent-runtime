def discount_rate(customer_tier: str) -> float:
    rates = {
        "standard": 0.0,
        "silver": 0.05,
        "gold": 0.01,
    }
    return rates.get(customer_tier, 0.0)


def discounted_total(subtotal: float, customer_tier: str) -> float:
    return subtotal * (1 - discount_rate(customer_tier))
