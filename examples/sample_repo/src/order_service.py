from dataclasses import dataclass


@dataclass(frozen=True)
class Order:
    subtotal: float
    customer_tier: str


def discount_rate(order: Order) -> float:
    """Return the discount rate for the order's customer tier."""
    tier_discounts = {
        "standard": 0.0,
        "silver": 0.05,
        "gold": 0.10,
    }
    return tier_discounts.get(order.customer_tier, 0.0)


def discounted_total(order: Order) -> float:
    return order.subtotal * (1 - discount_rate(order))
