import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { Badge, Kpi, ProbBar, Provenance } from "@/components/ui";
import { useLive } from "@/hooks/useLive";

afterEach(cleanup);

describe("UI primitives", () => {
  it("status badges always carry text (never colour alone)", () => {
    render(<Badge tone="critical">abnormal</Badge>);
    expect(screen.getByText("abnormal")).toBeTruthy();
  });

  it("renders KPI value and unit", () => {
    render(<Kpi label="Residual" value="+1.20" unit="t" />);
    expect(screen.getByText("+1.20")).toBeTruthy();
    expect(screen.getByText("t")).toBeTruthy();
  });

  it("renders probabilities as percentages", () => {
    render(<ProbBar p={0.4567} />);
    expect(screen.getByText("45.7%")).toBeTruthy();
  });

  it("labels data provenance", () => {
    render(<Provenance kind="simulated-truth" />);
    expect(screen.getByText("Simulated ground truth")).toBeTruthy();
  });
});

describe("live store", () => {
  it("ingests state and balance messages", () => {
    useLive.getState().ingest({ type: "balance", data: { t1: 1, facility_residual_t: 2, chi2: 3, dof: 4, p_value: 0.5, max_abs_z: 1, z: {}, anomaly: false, route: null } });
    expect(useLive.getState().balance?.facility_residual_t).toBe(2);
    expect(useLive.getState().events[0].type).toBe("balance");
    useLive.getState().ingest({ type: "anomaly", data: { event_id: "EVT-1" } });
    expect(useLive.getState().events[0].data.event_id).toBe("EVT-1");
  });
});
