"""Copy the core and AstrBot adapters to a fresh, separate runtime directory."""
import argparse
import json
from pathlib import Path
import re
import shutil
import tempfile


REPOSITORY = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {'.py', '.yaml', '.yml', '.json', '.md', '.txt', '.toml', '.ini',
                 '.html', '.css', '.js', '.mjs', '.ts', '.svg'}
IGNORE = shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo', '.git', '.cache')


def render_tree(root, identities):
    if not identities:
        return
    pattern = re.compile('|'.join(re.escape(key) for key in sorted(identities, key=len, reverse=True)))
    for path in root.rglob('*'):
        if not path.is_file() or (path.suffix and path.suffix.lower() not in TEXT_SUFFIXES):
            continue
        try:
            original = path.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            continue
        # Replace once: a rendered identity may equal a different example identifier.
        rendered = pattern.sub(lambda match: identities[match.group()], original)
        if rendered != original:
            path.write_text(rendered, encoding='utf-8')


def installation_plan(target, core_target=None, repository=REPOSITORY):
    repository = Path(repository).resolve()
    target = Path(target).expanduser().resolve()
    if core_target is None:
        if target.name != 'plugins' or target.parent.name != 'data':
            raise ValueError('Use <AstrBot>/data/plugins, or set --core-target <import-root>/dafeiyu')
        core_target = target.parent.parent / 'dafeiyu'
    core_target = Path(core_target).expanduser().resolve()
    if core_target.name != 'dafeiyu':
        raise ValueError('--core-target must name the dafeiyu package directory')
    for destination in (target, core_target):
        if (destination == repository or repository in destination.parents
                or destination in repository.parents):
            raise ValueError('Use an installation directory outside the source repository')
    if target == core_target or target in core_target.parents or core_target in target.parents:
        raise ValueError('Core must be outside the AstrBot plugin discovery directory')
    plan = [(repository / 'src/dafeiyu', core_target)]
    plan += [(source, target / source.name) for source in sorted((repository / 'plugins').iterdir())
             if source.is_dir() and not source.name.startswith('.') and source.name != '__pycache__']
    for source, destination in plan:
        if not source.is_dir():
            raise ValueError(f'Missing source directory: {source.name}')
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(f'Target already exists; review an upgrade separately: {destination}')
    return plan


def install(target, identities, core_target=None, repository=REPOSITORY):
    if (not isinstance(identities, dict)
            or not all(str(key).isdigit() and str(value).isdigit() for key, value in identities.items())):
        raise ValueError('Identity mapping must contain numeric identifiers only')
    identities = {str(key): str(value) for key, value in identities.items()}
    plan = installation_plan(target, core_target, repository)
    # Render everything before publishing destinations. Never read runtime databases.
    with tempfile.TemporaryDirectory(prefix='dafeiyu-install-') as temporary:
        staged = Path(temporary)
        for index, (source, _) in enumerate(plan):
            prepared = staged / str(index)
            shutil.copytree(source, prepared, ignore=IGNORE)
            render_tree(prepared, identities)
        for index, (_, destination) in enumerate(plan):
            # Refuse existing paths, including changes since preflight.
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(staged / str(index), destination)
    return [destination for _, destination in plan]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True, help='<AstrBot>/data/plugins')
    parser.add_argument('--identities', required=True, help='Private JSON identity mapping')
    parser.add_argument('--core-target', help='Optional <Python import root>/dafeiyu directory')
    args = parser.parse_args()
    try:
        identities = json.loads(Path(args.identities).read_text(encoding='utf-8'))
        destinations = install(args.target, identities, args.core_target)
    except (OSError, ValueError) as error:
        parser.exit(1, f'Installation stopped: {error}\n')
    print(f'Copied core and {len(destinations) - 1} plugins. Core: {destinations[0]}')
    print('Run AstrBot from its runtime root. Configure credentials and QQ permissions before enabling.')


if __name__ == '__main__':
    main()
