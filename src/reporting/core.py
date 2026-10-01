"""Project existing results without evaluating rules or reconstructing runs."""

from dataclasses import dataclass
from pathlib import Path
import os
import shutil
import uuid

import duckdb


def schema(spec):
    return tuple(tuple(field.split(':')) for field in spec.split())


SCHEMAS = {
    'assets': schema('asset:VARCHAR node_id:VARCHAR'),
    'rules': schema('rule_id:VARCHAR asset:VARCHAR column_name:VARCHAR node_id:VARCHAR'),
    'dq_evaluations': schema('run_id:VARCHAR rule_id:VARCHAR started_at:TIMESTAMP data_zone:VARCHAR snapshot_cutoff:DATE asset:VARCHAR column_name:VARCHAR dimension:VARCHAR expectation_type:VARCHAR empty_policy:VARCHAR status:VARCHAR rows_total:BIGINT rows_evaluated:BIGINT rows_not_evaluated:BIGINT evaluation_ratio:DOUBLE violations:BIGINT conforming_evaluations:BIGINT violation_ratio:DOUBLE compliance_ratio:DOUBLE'),
    'dq_summaries': schema('summary_id:VARCHAR aggregation_level:VARCHAR run_id:VARCHAR data_zone:VARCHAR snapshot_cutoff:DATE asset:VARCHAR dimension:VARCHAR evaluations:BIGINT distinct_rules:BIGINT passed_evaluations:BIGINT failed_evaluations:BIGINT pass_ratio:DOUBLE row_rule_evaluations:DECIMAL(38,0) row_rule_violations:DECIMAL(38,0) row_rule_conforming:DECIMAL(38,0) weighted_violation_ratio:DOUBLE weighted_compliance_ratio:DOUBLE'),
    'governance_issues': schema('issue_id:VARCHAR asset:VARCHAR column_name:VARCHAR rule_id:VARCHAR status:VARCHAR description:VARCHAR'),
    'governance_decisions': schema('decision_id:VARCHAR issue_id:VARCHAR decision_type:VARCHAR status:VARCHAR policy_id:VARCHAR decision_date:DATE actor:VARCHAR rationale:VARCHAR policy_asset:VARCHAR policy_column:VARCHAR'),
    'execution_runs': schema('run_id:VARCHAR process_type:VARCHAR status:VARCHAR started_at:TIMESTAMP completed_at:TIMESTAMP elapsed_seconds:DOUBLE data_zone:VARCHAR snapshot_cutoff:DATE source_path:VARCHAR output_path:VARCHAR execution_record_path:VARCHAR'),
    'execution_details': schema('detail_id:VARCHAR run_id:VARCHAR detail_type:VARCHAR semantic_id:VARCHAR asset:VARCHAR column_name:VARCHAR status:VARCHAR rows_total:BIGINT rows_evaluated:BIGINT rows_changed:BIGINT violations:BIGINT compliance_ratio:DOUBLE input_rows:BIGINT output_rows:BIGINT attributes:VARCHAR'),
}
KEYS = {'assets': ('asset',), 'rules': ('rule_id',),
        'dq_evaluations': ('run_id', 'rule_id'), 'dq_summaries': ('summary_id',),
        'governance_issues': ('issue_id',), 'governance_decisions': ('decision_id',),
        'execution_runs': ('run_id',), 'execution_details': ('detail_id',)}
SOURCES = {'metrics': 'metrics/dq_metrics.duckdb',
           'governance': 'governance/governance_registry.duckdb',
           'traceability': 'traceability/execution_traceability.duckdb',
           'lineage': 'lineage/lineage.duckdb'}


@dataclass(frozen=True)
class ReportingConfig:
    root_path: Path

    @property
    def output_path(self):
        return self.root_path / 'data/results/reporting'


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def require_zero(connection, sql, message):
    if connection.execute(sql).fetchone()[0]:
        raise ValueError(message)


def project(connection, config):
    for alias, relative in SOURCES.items():
        path = (config.root_path / 'data/results' / relative).resolve()
        if not path.is_file():
            raise ValueError(f'Missing source: {path}')
        connection.execute(f'ATTACH {literal(path)} AS {alias} (READ_ONLY)')
    require_zero(connection, """SELECT count(*) FROM (
        SELECT semantic_id FROM lineage.lineage_nodes
        WHERE node_type='standardization_policy'
        GROUP BY semantic_id HAVING count(*)>1)""", 'Duplicate policy ID')
    queries = {
        'assets': "SELECT asset,node_id FROM lineage.lineage_nodes WHERE node_type='asset' AND zone='source'",
        'rules': "SELECT semantic_id AS rule_id,asset,column_name,node_id FROM lineage.lineage_nodes WHERE node_type='dq_rule'",
        'dq_evaluations': 'SELECT * FROM metrics.dq_evaluation_metrics',
        'dq_summaries': """SELECT sha256(to_json(list_value(aggregation_level,run_id,asset,dimension))) AS summary_id,*
            FROM metrics.dq_aggregate_metrics WHERE aggregation_level IN ('RUN','RUN_ASSET','RUN_DIMENSION')""",
        'governance_issues': 'SELECT * FROM governance.governance_issues',
        'governance_decisions': """SELECT d.*,p.asset AS policy_asset,p.column_name AS policy_column
            FROM governance.governance_decisions d LEFT JOIN lineage.lineage_nodes p
            ON p.node_type='standardization_policy' AND p.semantic_id=d.policy_id""",
        'execution_runs': 'SELECT * FROM traceability.execution_runs',
        'execution_details': 'SELECT * REPLACE(CAST(attributes AS VARCHAR) AS attributes) FROM traceability.execution_details',
    }
    for name, query in queries.items():
        expected = SCHEMAS[name]
        actual = [(row[0], row[1]) for row in connection.execute(f'DESCRIBE ({query})').fetchall()]
        source_expected = [(col, 'HUGEINT' if name=='dq_summaries' and col.startswith('row_rule_') else typ) for col,typ in expected]
        if actual != source_expected:
            raise ValueError(f'Unexpected source schema for {name}: {actual}')
        columns = ','.join(f'CAST({col} AS {typ}) AS {col}' for col,typ in expected)
        connection.execute(f'CREATE TABLE {name} AS SELECT {columns} FROM ({query})')


def validate(connection):
    for name, keys in KEYS.items():
        nulls = ' OR '.join(f'{k} IS NULL' for k in keys)
        require_zero(connection, f'SELECT count(*) FROM {name} WHERE {nulls}', f'NULL key: {name}')
        require_zero(connection, f"SELECT count(*) FROM (SELECT {','.join(keys)} FROM {name} GROUP BY ALL HAVING count(*)>1)", f'Duplicate grain: {name}')
    relations = [('rules','asset','assets','asset'), ('dq_evaluations','asset','assets','asset'),
        ('dq_evaluations','rule_id','rules','rule_id'), ('governance_issues','asset','assets','asset'),
        ('governance_issues','rule_id','rules','rule_id'), ('governance_decisions','issue_id','governance_issues','issue_id'),
        ('dq_evaluations','run_id','execution_runs','run_id'), ('dq_summaries','run_id','execution_runs','run_id'),
        ('execution_details','run_id','execution_runs','run_id'), ('execution_details','asset','assets','asset'),
        ('dq_summaries','asset','assets','asset')]
    for child, fk, parent, pk in relations:
        require_zero(connection, f'SELECT count(*) FROM {child} c LEFT JOIN {parent} p ON c.{fk}=p.{pk} WHERE c.{fk} IS NOT NULL AND p.{pk} IS NULL', f'Invalid FK: {child}.{fk}')
    for table in ('dq_evaluations','governance_issues'):
        require_zero(connection, f'SELECT count(*) FROM {table} e JOIN rules r USING(rule_id) WHERE e.asset IS DISTINCT FROM r.asset OR e.column_name IS DISTINCT FROM r.column_name', f'Rule scope mismatch: {table}')
    require_zero(connection, """SELECT count(*) FROM dq_summaries WHERE
        run_id IS NULL OR aggregation_level NOT IN ('RUN','RUN_ASSET','RUN_DIMENSION') OR
        (aggregation_level='RUN' AND (asset IS NOT NULL OR dimension IS NOT NULL)) OR
        (aggregation_level='RUN_ASSET' AND (asset IS NULL OR dimension IS NOT NULL)) OR
        (aggregation_level='RUN_DIMENSION' AND (asset IS NOT NULL OR dimension IS NULL))""", 'Invalid summary grain')
    require_zero(connection, """SELECT count(*) FROM governance_decisions d
        JOIN governance_issues i USING(issue_id) WHERE d.policy_id IS NOT NULL
        AND (d.policy_asset IS NULL OR d.policy_column IS NULL
          OR d.policy_asset IS DISTINCT FROM i.asset OR d.policy_column IS DISTINCT FROM i.column_name)""", 'Missing or mismatched policy scope')
    require_zero(connection, """SELECT count(*) FROM dq_evaluations e JOIN execution_runs r USING(run_id)
        WHERE r.process_type<>'dq' OR e.started_at IS DISTINCT FROM r.started_at
        OR e.data_zone IS DISTINCT FROM r.data_zone OR e.snapshot_cutoff IS DISTINCT FROM r.snapshot_cutoff""", 'DQ run identity mismatch')
    require_zero(connection, """SELECT count(*) FROM dq_summaries e JOIN execution_runs r USING(run_id)
        WHERE r.process_type<>'dq' OR e.data_zone IS DISTINCT FROM r.data_zone
        OR e.snapshot_cutoff IS DISTINCT FROM r.snapshot_cutoff""", 'Summary run identity mismatch')


def cleanup(path, parent):
    # Only remove generated sibling directories within the verified results parent.
    if path.resolve().parent != parent.resolve():
        raise ValueError('Cleanup path escaped publication parent')
    if path.exists():
        shutil.rmtree(path)


def build(config):
    output = config.output_path.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    staging = output.with_name(f'.reporting-staging-{token}')
    backup = output.with_name(f'.reporting-backup-{token}')
    published = moved = False
    try:
        staging.mkdir()
        with duckdb.connect(':memory:') as connection:
            project(connection, config)
            validate(connection)
            counts = {}
            for name, expected in SCHEMAS.items():
                path = staging / f'{name}.parquet'
                order = ','.join(KEYS[name])
                connection.execute(f'COPY (SELECT * FROM {name} ORDER BY {order}) TO {literal(path)} (FORMAT PARQUET, COMPRESSION ZSTD)')
                source = f'read_parquet({literal(path)})'
                actual = [(r[0],r[1]) for r in connection.execute(f'DESCRIBE SELECT * FROM {source}').fetchall()]
                if actual != list(expected):
                    raise ValueError(f'Parquet schema mismatch: {name}')
                require_zero(connection, f'SELECT count(*) FROM ((SELECT * FROM {name} EXCEPT ALL SELECT * FROM {source}) UNION ALL (SELECT * FROM {source} EXCEPT ALL SELECT * FROM {name}))', f'Parquet roundtrip mismatch: {name}')
                counts[name] = connection.execute(f'SELECT count(*) FROM {source}').fetchone()[0]
        if {p.name for p in staging.iterdir()} != {f'{n}.parquet' for n in SCHEMAS}:
            raise ValueError('Publication must contain exactly eight Parquet files')
        if output.exists():
            os.replace(output, backup)
            moved = True
        try:
            os.replace(staging, output)
        except Exception:
            if moved:
                os.replace(backup, output)
            raise
        published = True
        return counts
    finally:
        for path in (staging, backup):
            # Retain backup if rollback itself failed; never destroy previous output.
            if path == backup and moved and not published and not output.exists():
                continue
            try:
                cleanup(path, output.parent)
            except Exception:
                if not published:
                    raise


def run_reporting(config):
    result = dict(status='FAILED', counts={}, errors=[])
    try:
        result['counts'] = build(config)
        result['status'] = 'SUCCESS'
    except Exception as exc:
        result['errors'].append(f'{type(exc).__name__}: {exc}')
    return result
