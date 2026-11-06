"""Run isolated core and native adapter regressions without real QQ transports."""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--core', action='store_true', help='Check the core without AstrBot on sys.path')
    parser.add_argument('tests', nargs='*', help='Optional test filenames')
    args = parser.parse_args()
    names = args.tests or (['test_core_architecture.py'] if args.core else
                           sorted(path.name for path in (ROOT / 'tests').glob('test_*.py')))
    for name in names:
        if Path(name).name != name or not (ROOT / 'tests' / name).is_file():
            parser.error(f'Unknown test filename: {name}')
    failed = []
    total = 0
    with tempfile.TemporaryDirectory(prefix='dafeiyu-tests-') as temporary:
        work = Path(temporary)
        (work / 'data/plugins').mkdir(parents=True)
        paths = [ROOT / 'src', work, ROOT / 'plugins']
        if not args.core:
            paths.append(ROOT / 'framework')
        env = dict(os.environ, ASTRBOT_ROOT=str(work), REFACTOR_STAGE=str(ROOT / 'plugins'),
                   PYTHONIOENCODING='utf-8', PYTHONPATH=os.pathsep.join(map(str, paths)))
        for name in names:
            result = subprocess.run(
                [sys.executable, str(ROOT / 'tests/run_isolated.py'),
                 str(ROOT / 'tests' / name), str(ROOT / 'plugins')],
                cwd=work, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace')
            output = result.stdout + result.stderr
            matches = re.findall(r'Ran (\d+) tests? in ([^\n]+)', output)
            count = int(matches[-1][0]) if matches else 0
            total += count
            detail = f'{count} tests, {matches[-1][1]}' if matches else 'no test summary'
            print(f'[{"PASS" if result.returncode == 0 else "FAIL"}] {name}: {detail}', flush=True)
            if result.returncode:
                failed.append(name)
                print(output, flush=True)
    print(f'{total} tests across {len(names)} suites; {len(failed)} failing suites.', flush=True)
    return int(bool(failed))


if __name__ == '__main__':
    raise SystemExit(main())
