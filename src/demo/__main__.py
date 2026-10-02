"""Run the existing CLI stages without replacing their execution contracts."""

import argparse
from pathlib import Path
import shlex
import subprocess
import sys

import yaml


ROOT = Path(__file__).resolve().parents[2]
CUTOFFS = tuple(f'{year}-12-31' for year in range(1993, 1999))
EVIDENCE = (
    'data/results/dq/20261001T013557.424850Z-48654f22.json',
    'data/results/dq/20261001T023050.406156Z-b6e62a52.json',
    'data/results/standardization/std-20261001T022159.952149Z-88fe0f44.json',
)
REQUIRED = (
    'config/dataset_manifest.yaml', 'metadata/relationships.yaml',
    'metadata/replay/historical_snapshot.yaml',
    'metadata/governance/issues.yaml', 'metadata/governance/decisions.yaml',
    'metadata/dq_rules/validity.yaml', 'metadata/dq_rules/uniqueness.yaml',
    'metadata/dq_rules/referential_integrity.yaml',
    'metadata/standardization/card.yaml',
)


def preflight(root, source):
    if not source.is_dir():
        raise ValueError(f'Source directory is missing: {source}')
    for relative in (*REQUIRED, *EVIDENCE):
        if not (root / relative).is_file():
            raise ValueError(f'Required file is missing: {relative}')
    manifest = yaml.safe_load((root / 'config/dataset_manifest.yaml').read_text(encoding='utf-8'))
    files = manifest['dataset']['files']
    if not isinstance(files, list) or not files:
        raise ValueError('Manifest must declare source files')
    for item in files:
        name = item['name']
        if not isinstance(name, str) or Path(name).name != name:
            raise ValueError('Invalid manifest source filename')
        if not (source / name).is_file():
            raise ValueError(f'Source file is missing: {name}')
        catalog = root / 'metadata/catalog' / (Path(name).stem + '.yaml')
        if not catalog.is_file():
            raise ValueError(f'Catalog file is missing: {catalog.relative_to(root)}')


def stages(source):
    yield 'Ingestion', 'ingestion', ['--source', str(source)]
    yield 'Profiling', 'profiling', []
    yield 'Catalog validation', 'catalog', []
    yield 'RAW DQ', 'dq', []
    yield 'Standardization / TRUSTED publication', 'standardization', []
    yield 'TRUSTED DQ', 'dq', ['--data-path', 'data/trusted', '--data-zone', 'trusted']
    for cutoff in CUTOFFS:
        yield f'Replay {cutoff}', 'replay', ['--cutoff', cutoff]
        yield f'Snapshot DQ {cutoff}', 'dq', [
            '--data-path', f'data/snapshots/{cutoff}', '--data-zone', 'snapshot',
            '--snapshot-cutoff', cutoff]
    for name, module in (
        ('Structural lineage', 'lineage'), ('Execution traceability', 'traceability'),
        ('DQ observability', 'observability'), ('DQ metrics / temporal observability', 'metrics'),
        ('Governance validation / registry', 'governance'), ('Reporting datasets', 'reporting'),
    ):
        yield name, module, []


def run_demo(source, root=ROOT):
    # Interpret a relative source against the caller's directory before changing cwd.
    source = Path(source).resolve()
    root = Path(root).resolve()
    try:
        preflight(root, source)
    except Exception as exc:
        print(f'Preflight: FAILED — {exc}', flush=True)
        return 1
    print('Preflight: SUCCESS', flush=True)
    for name, module, arguments in stages(source):
        command = [sys.executable, '-m', f'src.{module}', *arguments]
        print(f'{name}: {shlex.join(command)}', flush=True)
        try:
            result = subprocess.run(command, cwd=root, check=False)
        except OSError as exc:
            print(f'{name}: FAILED — {exc}', flush=True)
            return 1
        if result.returncode != 0:
            print(f'{name}: FAILED (exit {result.returncode})', flush=True)
            return result.returncode if result.returncode > 0 else 1
        print(f'{name}: SUCCESS', flush=True)
    print('End-to-end demo: SUCCESS', flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description='Run the framework demo through reporting.')
    parser.add_argument('--source', required=True, type=Path, help='External Berka source directory.')
    return run_demo(parser.parse_args().source)


if __name__ == '__main__':
    raise SystemExit(main())
