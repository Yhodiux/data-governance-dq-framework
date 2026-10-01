from pathlib import Path

from .core import ReportingConfig, run_reporting


def main():
    result = run_reporting(ReportingConfig(Path('.')))
    print(result['status'])
    for dataset, count in result['counts'].items():
        print(f'{dataset}.parquet: {count}')
    for error in result['errors']:
        print(error)
    return 0 if result['status'] == 'SUCCESS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
