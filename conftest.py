"""Test configuration.

This file is mostly here for its location: a conftest.py at the repo root is what
puts that root on sys.path, so `tests/` can `import harness`. Boring beats a
packaging config.
"""

from harness.state import TesterFailure, TestResult

# `TestResult` and `TesterFailure` are Pydantic models, but they match pytest's
# `Test*` collection glob, so importing them into a test module raises a
# PytestCollectionWarning. Setting the flag once here covers every test file --
# the alternative is repeating it in each one, and neither belongs in state.py.
TestResult.__test__ = False
TesterFailure.__test__ = False
