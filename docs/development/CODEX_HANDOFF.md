# Codex handoff

## 1. Estado actual del proyecto

- Último commit verificado: `2a54a41 feat: add trusted data quality revalidation`.
- Pasos completados: 0, 1A, 1B, 2, 3, 4, 5 y 5B.
- Suite actual: 82 pruebas exitosas.
- Arquitectura implementada:
  - Ingesta local desde SOURCE hacia RAW, con validaciones, conteos, SHA-256 y publicación atómica.
  - Perfilado físico de RAW mediante DuckDB, con resultados JSON.
  - Catálogo gobernado por metadatos YAML para los ocho activos y validación de relaciones.
  - Motor de calidad basado en reglas YAML y ejecutado con DuckDB.
  - Observabilidad persistida en DuckDB mediante ejecuciones y resultados por regla.
  - Estandarización gobernada por metadatos desde RAW hacia TRUSTED, sin modificar RAW.
  - Revalidación con el mismo motor de calidad sobre rutas y zonas configurables (`raw` y `trusted`).

## 2. Resultado del Paso 5B

La revalidación histórica produjo los siguientes resultados:

| Zona | Reglas | PASSED | FAILED |
|---|---:|---:|---:|
| RAW | 20 | 16 | 4 |
| TRUSTED | 20 | 17 | 3 |

La regla `CAR-VAL-002` pasó de 892 incumplimientos en RAW a 0 en TRUSTED.

Incumplimientos restantes en TRUSTED:

- `ORD-VAL-001`: 1,379.
- `TRA-VAL-002`: 16,666.
- `TRA-VAL-004`: 53,433.

Los registros históricos de observabilidad anteriores al Paso 5B permanecen sin `data_zone` ni `data_path`. Las ejecuciones nuevas registran esos campos para distinguir RAW y TRUSTED. Nunca se debe inferir retrospectivamente `data_zone` para registros legacy.

## 3. Próximo trabajo: Paso 6A — Historical Snapshot Builder

El Paso 6A todavía no está implementado. Su objetivo es construir snapshots históricos desde TRUSTED a partir de una fecha de corte configurable.

Principio rector:

> Usar tiempo sólo donde existe evidencia temporal documentada; usar relaciones para mantener coherencia donde no existe fecha. Nunca inventar vigencias.

## 4. Modelo temporal acordado

Activos con evidencia temporal documentada:

- `account.date`.
- `trans.date`.
- `loan.date`.
- `card.issued`.

Activos sin fecha de vigencia documentada:

- `client`.
- `disp`.
- `order`.
- `district`.

`client.birth_number` no es una fecha de vigencia bancaria y no debe utilizarse como tal.

Estrategias genéricas previstas:

- `temporal`.
- `reference`.
- `temporal_and_reference`.
- `static`.

Reglas por activo:

| Activo | Regla de selección |
|---|---|
| `account` | `date <= cutoff` |
| `disp` | `account_id` pertenece al snapshot de `account` |
| `client` | `client_id` pertenece al snapshot de `disp` |
| `order` | `account_id` pertenece al snapshot de `account` |
| `trans` | `date <= cutoff` y `account_id` pertenece al snapshot de `account` |
| `loan` | `date <= cutoff` y `account_id` pertenece al snapshot de `account` |
| `card` | `issued <= cutoff` y `disp_id` pertenece al snapshot de `disp` |
| `district` | Copia estática completa |

Las dependencias deben resolverse independientemente del orden en que aparezcan los activos en YAML.

## 5. Fuente y comportamiento del replay

- Fuente exclusiva: `data/trusted`.
- TRUSTED debe tratarse como solo lectura.
- El replay selecciona filas; no estandariza, limpia ni remedia datos.
- La construcción del snapshot no debe mutar artefactos de entrada.

## 6. Rangos temporales observados y fechas de demostración

Rangos observados actualmente en TRUSTED:

| Activo | Mínimo | Máximo |
|---|---|---|
| `account` | 1993-01-01 | 1997-12-29 |
| `trans` | 1993-01-01 | 1998-12-31 |
| `loan` | 1993-07-05 | 1998-12-08 |
| `card` | 1993-11-07 | 1998-12-29 |

Estos rangos son observaciones del conjunto actual, no reglas del motor.

Para demostraciones se usarán cierres anuales de 1993 a 1998. El motor debe aceptar cualquier fecha de corte válida.

Interfaz conceptual:

```text
python -m src.replay --cutoff YYYY-MM-DD
```

## 7. Metadatos futuros

Archivo previsto:

```text
metadata/replay/historical_snapshot.yaml
```

Debe declarar, por activo:

- Estrategia de selección.
- Columna temporal y formato, cuando corresponda.
- Dependencias y referencias.

La lógica no debe codificarse de forma rígida por nombre de activo; el comportamiento debe derivarse de los metadatos.

## 8. Salidas y registro de ejecución

Ruta de publicación del snapshot:

```text
data/snapshots/<cutoff>/
```

Cada snapshot debe incluir los ocho activos.

Registro de ejecución:

```text
data/results/replay/<run-id>.json
```

Campos mínimos previstos:

- `run_id`.
- Marcas de tiempo de inicio y fin.
- Duración.
- Estado.
- `cutoff`.
- `source_zone`.
- `source_path`.
- `snapshot_path`.
- Estrategia por activo.
- Filas de entrada y salida por activo.
- Errores.

## 9. Propiedades y pruebas requeridas para el Paso 6A

- Determinismo para la misma entrada y fecha de corte.
- Ausencia de fuga de datos futuros.
- Coherencia referencial entre los activos incluidos.
- Inmutabilidad de TRUSTED.
- Ausencia de transformaciones, limpieza o remediación durante el replay.
- Preservación completa de `district`.
- Ausencia de fechas de vigencia inventadas.
- Resolución de dependencias independiente del orden del YAML.
- Detección y manejo explícito de ciclos y dependencias inválidas.
- Publicación atómica y segura del snapshot.

## 10. Fuera de alcance del Paso 6A

- Ejecutar DQ sobre snapshots.
- Incorporar `data_zone=snapshot` al motor DQ.
- Incorporar `snapshot_cutoff` a los resultados DQ.
- Observabilidad temporal de snapshots.
- Dashboards.
- Scoring.
- Azure o Purview.
- Streaming.
- Airflow.
- Nuevas reglas DQ.
- Nuevas reglas de estandarización.

El Paso 6B cubrirá DQ sobre snapshots. El Paso 6C podrá cubrir observabilidad temporal de manera opcional.
