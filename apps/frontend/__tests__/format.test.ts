import { describe, expect, it } from "vitest";
import { fmt, humanize, pct, signed, statusTone, zSeverity } from "@/lib/format";
import { pointAt, polylineLength } from "@/digital-twin/geometry";

describe("format helpers", () => {
  it("formats numbers and missing values", () => {
    expect(fmt(1.23456, 2)).toBe("1.23");
    expect(fmt(null)).toBe("—");
    expect(fmt(5, 1, "t")).toBe("5.0 t");
    expect(pct(0.256)).toBe("25.6%");
    expect(signed(1.5)).toBe("+1.50");
    expect(signed(-0.25, 1)).toBe("-0.3");
  });

  it("maps z-scores to alarm severities", () => {
    expect(zSeverity(1.2)).toBe("normal");
    expect(zSeverity(-3)).toBe("warning");
    expect(zSeverity(6.5)).toBe("critical");
  });

  it("maps statuses to tones", () => {
    expect(statusTone("CLOSED")).toBe("normal");
    expect(statusTone("AWAITING_VERIFICATION")).toBe("warning");
    expect(statusTone("UNKNOWN_CAUSE")).toBe("critical");
    expect(statusTone("STOPPED")).toBe("offline");
    expect(humanize("PIPELINE_LEAK")).toBe("Pipeline Leak");
  });
});

describe("twin geometry", () => {
  const path: [number, number, number][] = [[0, 0, 0], [10, 0, 0], [10, 0, 10]];
  it("computes polyline length", () => {
    expect(polylineLength(path)).toBeCloseTo(20);
  });
  it("interpolates instrument positions along a pipeline", () => {
    const mid = pointAt(path, 0.5);
    expect(mid.x).toBeCloseTo(10);
    expect(mid.z).toBeCloseTo(0);
    const q = pointAt(path, 0.75);
    expect(q.z).toBeCloseTo(5);
  });
});
