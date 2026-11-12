# Mathematical Models

This document records the equations as implemented, with the module that implements each
one. Section numbers (§) refer to the architecture specification.

## 1. Process simulator (digital twin physics)

### 1.1 Product properties — `simulation/process/properties.py`

Simplified volume correction (§4.3), with T_ref = 15 °C:

ρ_T = ρ₁₅ / (1 + α(T − T_ref))  V₁₅ = V_T / (1 + α(T − T_ref))

Viscosity: ν(T) = ν₁₅ · exp(−b(T − 15)).

Streams mix exactly by mass. A tank's density and composition after a receipt is the mass
average of its contents and the receipt.

**Known simplification:** this is not the API MPMS Ch. 11.1 VCF. The `density_at` and
`vcf` interfaces are isolated so that the correlation can be replaced.

### 1.2 Tanks — `simulation/tanks/tank.py`

Lumped, well-mixed vertical cylinder with a flat bottom. The state is mass M, standard
volume V₁₅, enthalpy and composition:

dM/dt = Σṁ_in − Σṁ_out − ṁ_loss                         (§4.1)
V_obs = V₁₅ (1 + α(T − 15)),  h = V_obs / A             (§4.2)
d(M c_p T)/dt = Σ ṁ_in c_p,in T_in − ṁ_out c_p T + UA (T_amb − T)

A floor leak uses the orifice law driven by the liquid head:
ṁ_loss = ρ C_d A_hole √(2 g h).

### 1.3 Hydraulic network — `simulation/hydraulics/network.py`

Quasi-steady node-head formulation (§4.4):

- Tank nozzles are fixed-head nodes (head = liquid surface elevation). Manifolds and leak
  points are free nodes.
- Each pipe segment: Δh = (f L/D + ΣK + K_valve) · v|v| / 2g, with Q = vA.
- Darcy friction factor from Swamee–Jain (turbulent) or 64/Re (laminar), updated by
  fixed-point iteration.
- Pump branch: Q from the inverse pump curve, with a check valve.
- Leak sink: Q = C_d A_hole √(2 g (h − z)).
- Node balances are solved with MINPACK `hybr`. Mass is then routed through the solved
  volumetric flows with exact stream mixing, so the solver tolerance never leaks into the
  mass balance. Simulated conservation error is about 10⁻⁸ kg over a run.

**Limitation:** incompressible flow; line-pack and hydraulic transients are neglected.

### 1.4 Pumps — `simulation/pumps/pump.py` (§4.5)

H(Q) = h_f (s² H₀ − aQ²),  s = n / n_rated,  h_f = head-degradation factor (1 when healthy)
η(Q) = e_f η_BEP (2r − r²),  r = Q / (s Q_BEP)
P_shaft = ρ g Q H / η,  P_elec = P_shaft / η_motor

Pump degradation lowers h_f and e_f, which gives an independent signal through measured
power and discharge pressure.

### 1.5 Valves — `simulation/valves/valve.py`

Rate-limited actuator with an equal-percentage characteristic:

Cv(x) = Cv_max R^(x−1) for x ≥ x_min, linear to zero below x_min
K(x) = k_open (Cv_max / Cv(x))²

A passing seat is a minimum effective Cv fraction that remains when the valve is
commanded closed.

### 1.6 Flow control

PI control on the **measured** flow of the route's control meter, acting on the control
valve. Because the loop controls measured flow, a meter bias on the control meter changes
the true transfer rate, as it would in a real plant.

### 1.7 Instruments — `simulation/sensors/sensor.py`

y = x(1 + e_rel,sys + b_rel(t)) + e_abs,sys + b_abs(t) + ε_white + ε_AR(1)

followed by low-flow cut-off, range saturation and quantisation.

- e_sys is drawn once per run from the instrument accuracy.
- b(t) is an injected fault: a constant bias, or a drift growing with time.
- Stuck holds the last value. Dropout stops publication.
- Flow meters also integrate a gross-volume totalizer, which keeps volume accounting
  robust to communication gaps.
- The fault state is never published. It exists only in ground truth.

## 2. Measurement model — `intelligence/physics/measurement.py`

This layer uses only what a real plant has: telemetry and operational records.

**Tank inventory.**
level → V_obs (strapping) → V₁₅ = V_obs · VCF(T_tank) → M = V₁₅ ρ̂₁₅(t).

ρ̂₁₅(t) starts from the latest lab sample and is propagated by mixing in metered receipts,
weighted by the line density meter. A new lab sample re-anchors the *next* window and never
retroactively changes the current one, so it does not create a spurious inventory jump.

**Meter mass (flow-computer method).** Gross-volume increments come from the totalizer and
are converted per 60 s step:

M = Σ ΔV · CTL(T_line) · ρ₁₅ / 1000,  CTL = 1 / (1 + α(T_line − 15))

using the temperature and density instruments associated with the meter. While one of
those instruments drops out, the last known value is held, as a flow computer would.

**Boundary interpolation error.** A transfer record boundary at fraction f of a historian
interval (h seconds) splits that interval's increment ΔM by linear interpolation. The true
split is unknown, so the 1σ error is

σ_b = ΔM · (h − h_cal)/h · √((½ − f)² + 1/12)

Here h_cal = 10 s is the sample spacing of the calibration data; at that spacing the
effect is already inside the empirical ledger and ticket terms. ΔM is the increment
actually observed across the bracketing samples, not a typical flow rate, so a start-up
ramp that no in-window sample has seen yet is still covered.

## 3. Topology-aware mass balance — `intelligence/mass_balance/`

### 3.1 Control volumes (`topology.py`)

The facility graph G = (V, E) is cut at:

- every flow meter that registered flow in the window;
- every valve that stayed shut for the whole window.

Each connected component is a control volume with an explicit measurement boundary:

R_cv = Σ metered inflow − Σ metered outflow − Σ ΔM_tanks in the volume

R > 0 means apparent loss. The control-volume residuals sum to the facility residual, so
they form a non-redundant basis of all valid balances (§4.6). A meter with no flow carries
no information and is not a cut.

### 3.2 Evidence rows (`engine.py`)

For consecutive windows [t₀, t₁, …, t_W], every row is linear in a set of keyed
measurement-error sources e:

r = r_true + J e,  e ~ N(0, diag σ²)

| Row | Meaning | Unit |
|---|---|---|
| CV | Control-volume mass balance | t |
| LEDGER | Metered mass not covered by any transfer record | t |
| TICKET | Booked quantity − custody-meter mass over the transfer | t |
| TCONS | Line temperature − source-tank temperature | °C |
| DCONS | Line density meter − source-tank density estimate | kg/m³ |
| PUMP | Measured pump head − curve head at the measured flow | m |

Error keys carry the correlation structure:

- Systematic calibration errors are shared across all windows (`FM-002:sys`).
- Gauge reading errors are keyed by boundary time point (`LT-002:read@3`) and shared by the
  two adjacent windows.
- Model errors are per row.

### 3.3 Uncertainty propagation (`intelligence/uncertainty/propagation.py`, §4.7)

Σ_r = J Σ_e Jᵀ,  z_i = r_i / √(Σ_r)_ii,  χ² = rᵀ Σ_r⁻¹ r

The detector alarms on the whitened χ² and on max |z|. The 1σ terms in `UncertaintyConfig`
were calibrated on normal-operation runs so that each row kind has unit-variance z. The
held-out check is in [results §6](../research/results.md).

## 4. Fault attribution (Model 3) — `intelligence/fault_attribution/bayesian.py`

### 4.1 Hypotheses (`hypotheses.py`)

The catalogue is derived from the plant topology:

- NORMAL;
- PIPELINE_LEAK and TANK_LEAK (physical loss, L);
- METER, LEVEL, TEMP and DENSITY errors, plus SENSOR_STUCK and SENSOR_DROPOUT
  (measurement, B);
- VALVE_PASSING and MISSING_TRANSFER (unrecorded movement, U);
- PUMP_DEGRADATION;
- UNKNOWN.

Each hypothesis has one target asset and a prior. Family prior mass is shared equally
across its targets.

### 4.2 Generative model

r = S_h θ + e,  e ~ N(0, Σ_r),  θ ~ N(0, C_h)

The signature S_h is built from the measurement topology of each window:

- which control volumes contain the leaking segment;
- which CV rows carry a meter with a calibration shift;
- which inventories a level or temperature error enters, through both boundary values and
  window averages;
- the sign of a passing valve's flow, from measured pressures.

Physical rates scale per sample with √(pressure) for line leaks (orifice law) and with
√(level) for tank leaks.

Sensor errors use a structured prior: pre-existing offset + step at onset + drift rate.
Unknown onset time is a mixture over sub-window positions. Unknown leak location is a
mixture over segments.

### 4.3 Marginal likelihood and posterior

p(r | H) = N(r; 0, Σ_r + S C Sᵀ)

The marginal is evaluated in closed form, using Woodbury and a Cholesky factorisation.
One-sided magnitudes (a leak cannot be negative) add the half-normal correction
log 2Φ(μ/s).

**Quality evidence.** Flat-lined or heavily gapped signals are nearly impossible without a
fault. They enter as likelihood ratios for SENSOR_STUCK and SENSOR_DROPOUT.

**UNKNOWN.** Modelled as N(0, Σ_r + diag(κσ_r)²), a broad alternative. Evidence that no known
signature explains is assigned here rather than forced onto a known cause.

**Two-fault hypotheses.** Pairs of the leading single hypotheses are scored with stacked
signatures. Their prior is P(a)P(b)·λ / (P(a) + P(b)).

**Posterior.**

P(H | E) = (1 − ε) · exp(ℓ_H/T + log P(H) − Z) + ε P(H)

T (temperature) and ε (prior mixing) are fitted on training and validation cases (§7.4).

**Residual decomposition** (R = L + B + U + ε, §4.6). The posterior-weighted fitted
signatures are projected onto the CV rows.

**Physical loss estimate.** Conditional on a physical-loss cause, this is the
posterior-weighted mean of the hypotheses' fitted loss. Its variance combines parameter
uncertainty, between-hypothesis spread and a calibrated relative model error. It is
reported (not null) only when P(physical loss) ≥ 0.6, with a 90% interval.

**Status rules.**

| Condition | Status |
|---|---|
| NORMAL is top with P ≥ 0.5 | NO_ABNORMALITY_CONFIRMED |
| P(unknown) ≥ 0.4, or UNKNOWN is top | UNKNOWN_CAUSE |
| Top hypothesis P ≥ 0.8 | PROBABLE_CAUSE_IDENTIFIED |
| Top-to-second ratio < 4 | COMPETING_HYPOTHESES |
| Otherwise | INSUFFICIENT_EVIDENCE |

## 5. Active diagnosis (Model 4) — `intelligence/active_diagnosis/`

Each verification action a has binary outcomes y ∈ {POSITIVE, NEGATIVE}, a sensitivity per
hypothesis family, and a false-positive rate (`tests.py`). Ranking (§7.5):

IG(a) = H(p) − Σ_y P(y) H(p | y),  P(y) = Σ_h p_h P(y | h)
score(a) = IG(a) / [(1 + cost_a)^0.5 · (1 + safety_risk_a)]

Unavailable actions score zero:

- actions already performed;
- isolation-requiring tests while a transfer is running.

After an engineer records the outcome, the posterior is updated by Bayes' rule with the
same likelihoods:

p'_h ∝ p_h P(y | h)

| Action | Cost | Duration | Confirms (sensitivity) |
|---|---|---|---|
| LEDGER_RECONCILIATION | 1 | 0.5 h | MISSING_TRANSFER 0.95 |
| INSTRUMENT_DIAGNOSTICS | 1 | 0.5 h | STUCK 0.97, DROPOUT 0.97 |
| RETRIEVE_METER_PROVING_RECORD | 4 | 3 h | METER_ERROR 0.93 |
| MANUAL_TANK_GAUGING | 2 | 1 h | LEVEL_ERROR 0.92, TEMP_ERROR 0.5 |
| REFERENCE_TEMPERATURE_CHECK | 1.5 | 1 h | TEMP_ERROR 0.95 |
| DENSITY_LAB_CHECK | 2 | 4 h | DENSITY_ERROR 0.9 |
| VALVE_SEAT_TEST | 5 | 2 h | VALVE_PASSING 0.9 (requires isolation) |
| LINE_INSPECTION | 6 | 3 h | PIPELINE_LEAK 0.85 |
| TANK_ISOLATION_TEST | 8 | 6 h | TANK_LEAK 0.85 (requires isolation) |
| PUMP_PERFORMANCE_TEST | 3 | 1.5 h | PUMP_DEGRADATION 0.95 |

## 6. Learned models (Models 1 and 2) — `intelligence/anomaly_detection/`, `intelligence/sensor_diagnostics/`

**Features** (`intelligence/features/windows.py`):

- Physics features: uncertainty-normalised residuals of the topology-derived rows, plus χ².
- Raw-signal features: flows, meter disagreement, level rates, pressures, pump power.

**Model 1 detectors**, compared against each other:

- Conventional threshold: facility discrepancy above a fixed percentage of throughput.
- χ² / max |z| statistical test.
- Isolation Forest, trained on normal windows only.
- XGBoost, supervised on labelled simulated windows.

**Model 2.** Per-instrument, per-window XGBoost classifier with classes NORMAL, BIAS_POS,
BIAS_NEG, DRIFT, STUCK and DROPOUT.

**Splits.** By complete simulation run, never by window. Environments:

- A: same distribution as training, held-out runs.
- B: new noise levels, severities and fault combinations.
- C: fault families never seen in training.

## 7. Known modelling limitations

- Simplified VCF instead of API MPMS Ch. 11.1. Flat-bottom strapping with no roof or
  bottom corrections.
- Quasi-steady hydraulics: no line-pack or transients.
- Linearised (first-order) uncertainty propagation.
- At coarse historian sampling, linear totalizer interpolation across start-up ramps biases
  the ticket rows. The boundary term bounds this bias but does not remove it.
- The synthetic data only show that the models recognise the faults this simulator models.
  Real diagnostic accuracy needs field validation (§18).
