# pricing

Order pricing for the storefront: catalogue lookup, volume discounts, and totals.

```
pricing/
  money.py        rounding to whole cents
  catalog.py      SKU -> Product
  discounts.py    volume tiers
  orders.py       order totals; uses all three
```

Amounts are `Decimal` everywhere. Multiply freely, round once at the boundary.

## Volume tiers

One tier applies to the whole order, chosen by total unit count. Bounds are
inclusive — an order of exactly 10 units is `bulk`.

| Tier        | Units | Off |
| ----------- | ----- | --- |
| standard    | 1+    | 0%  |
| bulk        | 10+   | 5%  |
| wholesale   | 50+   | 10% |
| distributor | 100+  | 15% |

## Tests

```
python -m pytest
```
