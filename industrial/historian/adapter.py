"""Read-only historian adapters (FR-016).

* FileHistorianAdapter : CSV / Parquet exports  (timestamp, tag, value[, quality])
* RestHistorianAdapter : generic REST historian in the style of PI Web API
                         GET {base}/streams/{web_id}/recorded?startTime=..&endTime=..

Both map historian tags to platform sensor ids via a tag map and emit the
internal telemetry contract with source=HISTORIAN. Neither can write.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from industrial.schemas.telemetry import TelemetryRecord, utcnow


def _rec(meta: dict[str, str], ts, value, quality: str, tag: str) -> TelemetryRecord:
    t = pd.Timestamp(ts)
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    return TelemetryRecord(asset_id=meta["asset_id"], sensor_id=meta["sensor_id"], parameter=meta["parameter"],
                           value=None if value is None or pd.isna(value) else float(value), unit=meta.get("unit", ""),
                           event_timestamp=t.to_pydatetime(), ingestion_timestamp=utcnow(), quality=quality,
                           source="HISTORIAN", source_tag=f"historian:{tag}")


class FileHistorianAdapter:
    def __init__(self, path: str | Path, tag_map: dict[str, dict[str, str]]):
        self.path = Path(path)
        self.tag_map = tag_map

    def test(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"ok": False, "error": f"{self.path} not found"}
        df = self._read(nrows=200)
        tags = sorted(df["tag"].astype(str).unique().tolist())
        return {"ok": True, "path": str(self.path), "columns": list(df.columns), "tags_sample": tags[:20],
                "mapped": [t for t in tags if t in self.tag_map]}

    def _read(self, nrows: int | None = None) -> pd.DataFrame:
        if self.path.suffix.lower() in (".parquet", ".pq"):
            df = pd.read_parquet(self.path)
            return df.head(nrows) if nrows else df
        return pd.read_csv(self.path, nrows=nrows)

    def read(self, start: datetime | None = None, end: datetime | None = None) -> list[TelemetryRecord]:
        df = self._read()
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        if start is not None:
            df = df[df["timestamp"] >= pd.Timestamp(start)]
        if end is not None:
            df = df[df["timestamp"] <= pd.Timestamp(end)]
        out = []
        for row in df.itertuples(index=False):
            meta = self.tag_map.get(str(row.tag))
            if meta is None:
                continue
            q = str(getattr(row, "quality", "GOOD") or "GOOD").upper()
            out.append(_rec(meta, row.timestamp, row.value, q if q in ("GOOD", "BAD", "UNCERTAIN") else "UNCERTAIN",
                            str(row.tag)))
        return out


class RestHistorianAdapter:
    def __init__(self, base_url: str, tag_map: dict[str, dict[str, str]], token: str | None = None,
                 timeout_s: float = 10.0, verify_tls: bool = True):
        self.base_url = base_url.rstrip("/")
        self.tag_map = tag_map
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.timeout_s = timeout_s
        self.verify_tls = verify_tls

    def test(self) -> dict[str, Any]:
        try:
            r = httpx.get(self.base_url, headers=self.headers, timeout=self.timeout_s, verify=self.verify_tls)
            return {"ok": r.status_code < 400, "status_code": r.status_code, "url": self.base_url}
        except httpx.HTTPError as e:
            return {"ok": False, "url": self.base_url, "error": repr(e)}

    def read(self, start: datetime, end: datetime) -> list[TelemetryRecord]:
        out = []
        with httpx.Client(headers=self.headers, timeout=self.timeout_s, verify=self.verify_tls) as c:
            for tag, meta in self.tag_map.items():
                r = c.get(f"{self.base_url}/streams/{tag}/recorded",
                          params={"startTime": start.astimezone(timezone.utc).isoformat(),
                                  "endTime": end.astimezone(timezone.utc).isoformat()})
                r.raise_for_status()
                for it in r.json().get("Items", []):
                    q = "GOOD" if it.get("Good", True) else "BAD"
                    out.append(_rec(meta, it["Timestamp"], it.get("Value"), q, tag))
        return out
