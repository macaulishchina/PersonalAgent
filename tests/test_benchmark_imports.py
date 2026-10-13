"""Pure benchmark accounting must not require optional training packages."""

import subprocess
import sys
import unittest


class BenchmarkImportTest(unittest.TestCase):
    def test_metrics_import_without_benchmark_extra(self):
        script = """
import importlib.abc
import sys
class BlockOptional(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {'pypinyin', 'psutil', 'torch', 'funasr'}:
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, BlockOptional())
from scripts.compare_wake_suite import summarize
from scripts.benchmark_asr_candidates import distance
from scripts.benchmark_light_wake import match
assert summarize([]) == {}
assert distance('a', 'a') == 0
assert match([], []) == (0, [])
"""
        subprocess.run([sys.executable, "-c", script], check=True, capture_output=True)
