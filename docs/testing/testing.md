# Testing and Validation

Testing happens at five levels (§13). Engineering validation (does the software behave?) and
model validation (does the diagnosis work?) are reported separately (§14.3).

## Running the tests

```bash
.venv/Scripts/python -m pytest -m "not slow"
```

This runs the Python unit and integration tests, about 35 s. The full MVP end-to-end leak
test takes about 2 minutes:

```bash
.venv/Scripts/python -m pytest -m slow
```

Frontend checks: unit tests (Vitest), type check and lint:

```bash
npm --prefix apps/frontend test
npm --prefix apps/frontend run typecheck
npm --prefix apps/frontend run lint
```

The research experiments regenerate the dataset, retrain the models and write
`docs/research/results.md`:

```bash
oil-experiments --regenerate
```

Backend API tests run against isolated temporary SQLite databases with per-session generated
credentials (`apps/backend/tests/conftest.py`). They never touch `data/`.

## 13.1 Unit tests

| Module | Tests (`tests/`) |
|---|---|
| Tank physics | `test_tank_mass_balance_step`, `test_tank_volume_level_mass_consistency`, `test_thermal_expansion_changes_level_not_mass` |
| Density model | `test_density_temperature_correction`, `test_stream_mixing_conserves_extensive_quantities` |
| Pipeline model | `test_friction_factor_regimes`, `test_pressure_flow_consistency_during_transfer` |
| Pump and valve models | `test_pump_curve_and_inverse`, `test_valve_characteristic_and_passing` |
| Sensor generator | `test_noise_statistics`, `test_drift_grows_with_time`, `test_stuck_and_dropout`, `test_low_flow_cutoff_and_quantisation`, `test_meter_bias_affects_measurement_not_physics` |
| Fault injection | `test_fault_injector_targets`, `test_missing_transaction_omits_record_but_moves_oil`, `test_unknown_fault_type_rejected` |
| Mass balance and topology | `test_topology_route_r01`, `test_topology_route_r03_merges_tank_and_manifold`, `test_inactive_meter_is_not_a_cut`, `test_sum_of_cv_residuals_equals_facility_residual`, `test_ticket_reconciliation_rows_present`, `test_ticket_skipped_when_transfer_starts_before_data` |
| Uncertainty | `test_uncertainty_propagation_matches_monte_carlo`, `test_normal_operation_residuals_within_uncertainty`, `test_sparse_sampling_widens_boundary_uncertainty` |
| Attribution and active diagnosis | `test_normal_run_attributed_normal`, `test_leak_attributed_to_physical_loss`, `test_bounded_magnitude_correction`, `test_silent_meter_bounds_passing_valve`, `test_information_gain_prefers_discriminating_check`, `test_isolation_tests_unavailable_during_transfer` |
| Learned models and metrics | `test_learned_detectors_train_and_score`, `test_metrics` |
| Agent tools | `test_tools_have_schemas_roles_and_no_control_path`, `test_schema_validation_and_permissions` |
| Frontend | `apps/frontend/__tests__/` (formatting, UI components) |

## 13.2 Integration tests

| Claim | Test |
|---|---|
| Simulator publishes telemetry on the bus | `test_inprocess_bus_and_live_simulation` |
| MQTT publish/subscribe, buffering while disconnected | `test_mqtt_publish_subscribe_with_embedded_broker`, `test_mqtt_buffers_while_disconnected` |
| OPC UA exposes the simulated values, read-only | `test_opcua_server_exposes_values_read_only` |
| Ingestion stores readings; backend serves them | `test_live_telemetry_flows`, `test_health_and_metrics` |
| Twin state matches the stored data | `test_twin_state_shape` |
| Run boundaries are respected | `test_live_run_clock_never_goes_backwards`, `test_analysis_never_spans_a_run_boundary` |
| Security: authentication, RBAC, audit, read-only | `test_endpoints_require_authentication`, `test_role_based_access`, `test_state_changes_are_audited`, `test_no_endpoint_can_command_plant_equipment`, `test_connectors_are_read_only`, `test_websocket_requires_token` |

## 13.3 End-to-end test (MVP acceptance, §19.1)

`apps/backend/tests/test_api_flow.py::test_end_to_end_leak_investigation` (marked `slow`)
drives the full sequence through the API at 120× real time:

1. Let 1.5 simulated hours of normal operation accumulate on the live schedule.
2. Inject a 9 mm leak on the active route's discharge line.
3. Wait for the anomaly event and the automatically opened investigation.
4. Re-analyse, and assert the leak is in the top-3 causes with a positive residual.
5. Perform the recommended checks through the simulation oracle. A resolved case must name
   the true cause with P > 0.9, and no wrong cause may reach 0.9.
6. Engineer confirms the cause; the case closes.
7. Assert the HTML and Markdown reports contain causes, evidence register, recommended
   verification and verification history.

## 13.4 Model validation

Splits are by simulation run (`intelligence/training/dataset.py`):

- **Environment A:** held-out runs from the training families.
- **Environment B:** new noise levels, edge-of-envelope rates, unseen fault combinations.
- **Environment C:** fault mechanisms absent from training (density bias, sensor noise, wrong
  ticket quantity).

Metrics follow §7.6. Results, KPI scorecard and limitations:
[docs/research/results.md](../research/results.md).

## 13.5 Agent evaluation

| Behaviour | Test |
|---|---|
| Missing sensor evidence → request it | `test_missing_evidence_requests_data`; `experiments/attribution/agent_eval.py` (`missing_level_gauge_requests_data`) |
| Unknown fault → no unsupported confident diagnosis | Environment C in `agent_eval` and the ablation (`confident_wrong_cases = 0`) |
| Failed database query → reported | `test_failed_query_reported` |
| Invalid equipment ID → rejected | `test_invalid_equipment_id_rejected` |
| Competing hypotheses preserved | `test_investigation_end_to_end_with_verification` |
| Unsupported corrective action → refused | `test_tools_have_schemas_roles_and_no_control_path` (no control tools exist) |
| Repeated investigation → no duplicate case | `test_repeated_investigation_not_duplicated`; `agent_eval` |
| Reviews and audit history preserved | `test_state_changes_are_audited` |
| Narrative grounding (every number traceable) | `test_grounding_check_rejects_invented_numbers`, `test_ungrounded_llm_narrative_is_discarded` |
