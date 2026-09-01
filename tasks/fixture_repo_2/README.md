# billing

Invoicing for the sales platform: tax, agent commission, and invoice totals.

```
billing/
  money.py        rounding to whole cents
  tax.py          sales tax by jurisdiction
  commission.py   what an agent earns on a sale
  invoices.py     invoice tax, totals, and payouts
```

Amounts are `Decimal` everywhere. Multiply freely, round once at the boundary.

## Two amounts, two rules

Money coming *in* from a customer and money going *out* to an agent do not round
the same way.

| Amount     | Rule                                  |
| ---------- | ------------------------------------- |
| Tax        | rounded to the nearest cent           |
| Commission | truncated to the cent, never rounded up |

## Tests

```
python -m pytest
```
