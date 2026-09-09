"""Run the protocol suite and reject an empty or entirely skipped run."""

from pathlib import Path
import sys
import unittest


root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
suite = unittest.defaultTestLoader.discover(str(root / "tests"))
result = unittest.TextTestRunner(verbosity=2).run(suite)
if result.testsRun <= len(result.skipped):
    sys.exit("No tests executed")
sys.exit(0 if result.wasSuccessful() else 1)
