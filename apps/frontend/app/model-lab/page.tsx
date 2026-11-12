"use client";

import { BarsChart } from "@/components/charts";
import { Card, Empty, JsonView, PageHeader, Spinner } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { fmt, humanize } from "@/lib/format";

function n(v: any, d = 3) {
  return typeof v === "number" ? fmt(v, d) : v ?? "—";
}

export default function ModelLab() {
  const { data, loading } = useApi<any>("/ml/model-performance");
  if (loading) return <Spinner />;
  const r = data?.results;
  if (!r) return <Empty>No evaluation results. Run <code>python -m experiments.run_experiments</code>.</Empty>;
  const m1 = r.model1_anomaly_detection ?? {};
  const m3 = r.model3_fault_attribution ?? {};
  const m4 = r.model4_active_diagnosis ?? {};
  const det = Object.entries<any>(m1.A ?? {}).map(([k, v]) => ({ name: humanize(k), F1: +v.f1.toFixed(3),
    "PR-AUC": +(v.pr_auc ?? 0).toFixed(3), "episode detection": +v.episode_detection_rate.toFixed(3) }));
  const attr = Object.entries<any>(m3.A ?? {}).filter(([k]) => k !== "physics_bayes_restricted_hypotheses").map(([k, v]) => ({
    name: humanize(k), "top-1": +(v.top1_family ?? 0).toFixed(3), "top-3": +(v.top3_family ?? 0).toFixed(3),
    "macro-F1": +(v.macro_f1_family ?? 0).toFixed(3) }));
  const ad = ["information_gain_per_cost", "information_gain", "random", "fixed_order"].filter((k) => m4[k]).map((k) => ({
    name: humanize(k), "tests to resolve": +m4[k].mean_tests.toFixed(2), "final accuracy": +m4[k].final_accuracy.toFixed(3),
    "resolution rate": +m4[k].resolution_rate.toFixed(3) }));
  return (
    <div className="space-y-4">
      <PageHeader title="Model Lab" subtitle={`Evaluation ${r.version} · ${r.counts.cases} simulated runs · ${r.counts.windows} windows · run-level splits, environments A (in-distribution), B (generalisation), C (unknown faults)`} />
      <div className="grid gap-4 xl:grid-cols-2">
        <Card title="Experiment 1/2 — anomaly detection (env A test)" subtitle="Observable-effect labels; thresholds tuned to ≤3 false alarms / 100 normal validation windows">
          <BarsChart data={det} keys={["F1", "PR-AUC", "episode detection"]} height={280} />
        </Card>
        <Card title="Experiment 3 — fault attribution (env A test)" subtitle="Physics-guided Bayesian vs learned classifiers (raw vs physics features)">
          <BarsChart data={attr} keys={["top-1", "top-3", "macro-F1"]} height={280} />
        </Card>
        <Card title="Experiment 4 — active diagnosis" subtitle="Simulated investigations on test cases (outcomes sampled from the check likelihood model)">
          <BarsChart data={ad} keys={["tests to resolve", "final accuracy", "resolution rate"]} height={260} />
        </Card>
        <Card title="Calibration & loss estimation">
          <div className="space-y-1 text-sm">
            <div className="flex justify-between"><span className="text-ink-3">Temperature T</span><span className="num">{n(r.model3_calibration?.temperature, 2)}</span></div>
            <div className="flex justify-between"><span className="text-ink-3">Prior mixing ε</span><span className="num">{n(r.model3_calibration?.prior_mix, 3)}</span></div>
            <div className="flex justify-between"><span className="text-ink-3">Loss model rel. error</span><span className="num">{n(r.model3_calibration?.loss_model_rel_error, 2)}</span></div>
            {["physics_bayes_uncalibrated", "physics_bayes_calibrated"].map((k) => m3.A?.[k] && (
              <div key={k} className="flex justify-between"><span className="text-ink-3">{humanize(k)} log-loss / Brier / ECE</span>
                <span className="num">{n(m3.A[k].log_loss)} / {n(m3.A[k].brier)} / {n(m3.A[k].ece)}</span></div>
            ))}
            <div className="mt-2 border-t border-line pt-2 text-xs uppercase text-ink-3">Physical loss estimate</div>
            {Object.entries<any>(r.loss_estimation ?? {}).filter(([k]) => typeof r.loss_estimation[k] === "object" && k !== "per_case").map(([k, v]) => (
              <div key={k} className="flex justify-between"><span className="text-ink-3">{humanize(k)} (n={v.n})</span>
                <span className="num">MAE {n(v.mae)} t · median err {n(v.median_abs_pct_error, 1)}% · coverage {n(v.interval_coverage, 2)}</span></div>
            ))}
          </div>
        </Card>
        <Card title="Unknown-fault handling (env C)">
          {m3.C ? Object.entries<any>(m3.C).filter(([k]) => k.startsWith("physics")).map(([k, v]) => (
            <div key={k} className="flex justify-between border-b border-line py-1 text-sm"><span className="text-ink-3">{humanize(k)}</span>
              <span className="num">abstention {n(v.abstention_rate, 2)} · confident wrong {v.confident_wrong_known_cause}</span></div>
          )) : <Empty>—</Empty>}
        </Card>
        <Card title="Instrument fault classifier (Model 2)">
          {Object.entries<any>(r.model2_sensor_diagnostics ?? {}).map(([env, v]) => (
            <div key={env} className="mb-2 text-sm">
              <div className="flex justify-between"><span className="text-ink-3">Env {env} macro-F1</span><span className="num">{n(v.macro_f1)}</span></div>
              <div className="num text-xs text-ink-3">{Object.entries<any>(v.per_class_recall).map(([c, x]) => `${c} ${fmt(x, 2)}`).join(" · ")}</div>
              {v.fault_detection_recall != null && (
                <div className="num text-xs text-ink-3">
                  faulty-window recall {fmt(v.fault_detection_recall, 2)} at {fmt((v.false_flag_rate ?? 0) * 100, 1)}% false flags
                  {v.decision ? ` (threshold ${fmt(v.decision.threshold, 3)} chosen on validation for ${fmt(v.decision.false_flag_budget * 100, 0)}%)` : ""}
                </div>
              )}
            </div>
          ))}
        </Card>
      </div>
      <Card title="Model registry">
        <JsonView value={{ registry: data.registry, loaded: data.loaded_models }} />
      </Card>
      <details><summary className="text-xs text-ink-3">Full results JSON</summary><JsonView value={r} /></details>
    </div>
  );
}
