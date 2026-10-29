from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SyncRequest:
    codes: list[str] = field(default_factory=lambda: ["000001.SZ"])
    start_date: str | None = None
    end_date: str | None = None
    lookback_trading_days: int = 5
    promote: bool = False
    batch_id: str | None = None


@dataclass
class ValidationIssue:
    code: str
    level: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class SyncResult:
    batch_id: str
    provider: str
    raw_dir: str
    staging_dir: str
    qlib_dir: str | None
    manifest_path: str
    report_path: str
    started_at: str
    finished_at: str
    success: bool
    codes_requested: int
    codes_succeeded: int
    codes_failed: int
    rows: int
    start_date: str | None
    end_date: str | None
    issues: list[ValidationIssue]
    elapsed_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "batchId": self.batch_id,
            "provider": self.provider,
            "rawDir": self.raw_dir,
            "stagingDir": self.staging_dir,
            "qlibDir": self.qlib_dir,
            "manifestPath": self.manifest_path,
            "reportPath": self.report_path,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "success": self.success,
            "codesRequested": self.codes_requested,
            "codesSucceeded": self.codes_succeeded,
            "codesFailed": self.codes_failed,
            "rows": self.rows,
            "startDate": self.start_date,
            "endDate": self.end_date,
            "issues": [
                {
                    "code": issue.code,
                    "level": issue.level,
                    "message": issue.message,
                    "details": issue.details,
                }
                for issue in self.issues
            ],
            "elapsedSeconds": self.elapsed_seconds,
        }
