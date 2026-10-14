#!/usr/bin/env python3
"""Runs the test suite: python3 run_tests.py [-v]"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from retry_alert._bootstrap import ensure_interpreter  # noqa: E402

ensure_interpreter(__file__)

if __name__ == '__main__':
    # top_level_dir must be the project root, not 'tests' itself: discover()
    # otherwise treats 'tests' as the import root, so test_engine.py's
    # `from .support import ...` has no parent package to resolve against and
    # fails with "attempted relative import with no known parent package".
    project_root = os.path.dirname(os.path.abspath(__file__))
    suite = unittest.defaultTestLoader.discover('tests', pattern='test_*.py',
                                                 top_level_dir=project_root)
    result = unittest.TextTestRunner(verbosity=2 if '-v' in sys.argv[1:] else 1).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
