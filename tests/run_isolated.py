import importlib
import os
import runpy
import sys

stage = os.environ['REFACTOR_STAGE']
sys.path.insert(0, stage)
for name in ('delivery', 'diagnostics'):
    shared = importlib.import_module('dafeiyu_runtime.' + name)
    sys.modules['data.plugins.dafeiyu_runtime.' + name] = shared
test = sys.argv[1]
sys.argv = [test] + sys.argv[2:]
runpy.run_path(test, run_name='__main__')
