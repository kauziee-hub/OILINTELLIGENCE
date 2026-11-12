# Datasets

Generated, not committed. Rebuild everything with:

```bash
oil-experiments --regenerate
```

To build only the dataset:

```bash
oil-dataset --out datasets --workers 4
```

| Folder | Contents |
|---|---|
| `synthetic/<run>/` | Full four-layer data for one held-out run per environment and family: `ground_truth.parquet` (physical truth), `telemetry.parquet` (sensor measurements), record tables (`transfers`, `calibrations`, `lab_samples`, `maintenance`, `operator_log`), `fault_injections.parquet` (injected faults), `scenario.json` (the reproducible scenario) and `events.json` |
| `training/` | Model inputs: `windows.parquet` (30-min balance windows), `sensor_windows.parquet` (per-instrument windows), `cases.jsonl` (investigation cases with attribution likelihoods and labels), `dataset_meta.json` (run plan, plan hash, plant hash) |
| `validation/`, `testing/`, `ground_truth/` | Reserved. Splits are recorded per run in `training/` (`split`, `env`) rather than as separate copies |

Every run is reproducible from its family, seed and environment. See
[the data dictionary](../docs/data_dictionary/data_dictionary.md#synthetic-dataset-layers).
