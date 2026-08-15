"""Test configuration.

This file is mostly here for its location: a conftest.py at the repo root is what
puts that root on sys.path, so `tests/` can `import harness`. Boring beats a
packaging config.
"""

from harness.agents.tester import Tester, tester_failure_from
from harness.state import TesterFailure, TestResult

# This project's vocabulary collides with pytest's collection globs: `Test*` for
# classes and `test*` for functions. None of these are tests -- `TestResult` and
# `TesterFailure` are Pydantic models, `Tester` is an agent, and
# `tester_failure_from` is a factory -- but importing them into a test module
# gets them collected, or warned about. Setting the flag once here covers every
# test file; the alternative is repeating it in each one, and none of it belongs
# in the modules themselves.
TestResult.__test__ = False
TesterFailure.__test__ = False
Tester.__test__ = False
tester_failure_from.__test__ = False
