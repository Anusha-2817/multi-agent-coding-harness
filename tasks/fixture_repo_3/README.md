# labels

Catalogue labels for the product listing: quantities, units, and how they read.

```
labels/
  plurals.py   singular -> plural
  units.py     unit names and abbreviations
  display.py   the label string a customer sees
```

## Plurals

Most words follow the rules in `_regular_plural`. `IRREGULAR` is a short table
of genuine exceptions to English -- child/children, foot/feet. A word the rules
get wrong is a bug in the rules, not a new row in the table.
