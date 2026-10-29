"""Two-phase causal intent protocol for the future-only intraday shadow run."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from .intraday_capture import list_verified_slot_records
from .intraday_shadow import (
    ARMS,
    PROJECT_ROOT,
    SHANGHAI_TZ,
    STATUS_VERSION,
    _atomic_json_write,
    _canonical_json,
    _parse_clock,
    _rank_signals,
    _resolve,
    _sha256_bytes,
    load_intraday_shadow_declaration,
)
from .intraday_shadow_v4 import (
    IntradayShadowCoordinator as V4IntradayShadowCoordinator,
)


DEFAULT_DECLARATION = (
    PROJECT_ROOT / "configs" / "intraday_shadow_20260814_t1_reversal_v5.json"
)
CAUSALITY_CONTRACT = {
    "version": 1,
    "decisionIntentRequired": True,
    "decisionFreezeAfter": "14:50:00",
    "decisionFreezeDeadline": "14:54:30",
    "executionBindingAfter": "14:56:00",
    "executionBindingDeadline": "15:05:30",
    "requireNoPriorPendingSession": True,
    "requirePortfolioFingerprint": True,
}
PENDING_INTENT = "DECISION_FROZEN"
BOUND_INTENT = "EXECUTION_BOUND"


class IntradayShadowCoordinator(V4IntradayShadowCoordinator):
    """Commit targets before the immutable execution quote can exist."""

    def __init__(self, declaration_path: Path | str = DEFAULT_DECLARATION):
        declaration, _ = load_intraday_shadow_declaration(declaration_path)
        causality = declaration.get("causality")
        if not isinstance(causality, Mapping):
            raise ValueError("intraday-shadow v5 causality contract is missing")
        if dict(causality) != CAUSALITY_CONTRACT:
            raise ValueError("intraday-shadow v5 causality contract drifted")
        timing = declaration.get("timing") or {}
        if (
            str(timing.get("freezeAfter"))
            != CAUSALITY_CONTRACT["executionBindingAfter"]
            or str(timing.get("freezeDeadline"))
            != CAUSALITY_CONTRACT["executionBindingDeadline"]
            or not (
                _parse_clock(str(timing.get("decisionSlot")), "decisionSlot")
                <= _parse_clock(
                    CAUSALITY_CONTRACT["decisionFreezeAfter"],
                    "decisionFreezeAfter",
                )
                <= _parse_clock(
                    CAUSALITY_CONTRACT["decisionFreezeDeadline"],
                    "decisionFreezeDeadline",
                )
                < _parse_clock(str(timing.get("executionSlot")), "executionSlot")
                < _parse_clock(str(timing.get("freezeAfter")), "freezeAfter")
                <= _parse_clock(str(timing.get("freezeDeadline")), "freezeDeadline")
            )
        ):
            raise ValueError("intraday-shadow v5 causal timing is invalid")
        self.causality = dict(CAUSALITY_CONTRACT)
        super().__init__(declaration_path)
        self._initialize_decision_intents()

    def _initialize_decision_intents(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS decision_intents(
                    trade_date TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    decision_record_sha256 TEXT NOT NULL,
                    decision_snapshot_sha256 TEXT NOT NULL,
                    decision_snapshot_path TEXT NOT NULL,
                    ranked_json TEXT NOT NULL,
                    targets_sha256_json TEXT NOT NULL,
                    portfolio_sha256 TEXT NOT NULL,
                    frozen_at TEXT NOT NULL,
                    execution_bound_at TEXT
                )
                """
            )

    def _slot_record_exists(self, trade_date: str, slot: str) -> bool:
        wanted = datetime.combine(
            date.fromisoformat(trade_date),
            _parse_clock(slot, "captureSlot"),
            SHANGHAI_TZ,
        ).isoformat()
        return any(
            record.get("captureSlot") == wanted
            for _, record in list_verified_slot_records(
                self.capture_root,
                trade_date,
            )
        )

    @staticmethod
    def _portfolio_sha256(connection: sqlite3.Connection) -> str:
        accounts = [
            dict(row)
            for row in connection.execute(
                "SELECT arm, initial_cash, cash, updated_at FROM accounts ORDER BY arm"
            )
        ]
        positions = [
            dict(row)
            for row in connection.execute(
                "SELECT arm, symbol, adjusted_quantity, factor, acquired_date, "
                "last_raw_price FROM positions ORDER BY arm, symbol"
            )
        ]
        return _sha256_bytes(
            _canonical_json({"accounts": accounts, "positions": positions}).encode(
                "utf-8"
            )
        )

    @staticmethod
    def _targets_sha256(ranked: Mapping[str, Any]) -> dict[str, str]:
        return {
            arm: _sha256_bytes(
                _canonical_json([row["symbol"] for row in ranked[arm]]).encode(
                    "utf-8"
                )
            )
            for arm in ARMS
        }

    @staticmethod
    def _signal_contract(
        ranked: Mapping[str, Any],
    ) -> dict[str, list[dict[str, Any]]]:
        return {
            arm: [
                {
                    "rank": int(row["rank"]),
                    "symbol": str(row["symbol"]),
                    "score": float(row["score"]),
                    "rawReturn": float(row["rawReturn"]),
                    "turnoverPercent": float(row["turnoverPercent"]),
                }
                for row in ranked[arm]
            ]
            for arm in ARMS
        }

    @staticmethod
    def _intent_ranked(row: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
        ranked = json.loads(str(row["ranked_json"]))
        if not isinstance(ranked, dict) or set(ranked) != set(ARMS):
            raise ValueError("intraday-shadow decision intent ranking is invalid")
        normalized = {arm: list(ranked[arm]) for arm in ARMS}
        if _canonical_json(normalized) != str(row["ranked_json"]):
            raise ValueError("intraday-shadow decision intent ranking is not canonical")
        expected_targets = IntradayShadowCoordinator._targets_sha256(normalized)
        if _canonical_json(expected_targets) != str(row["targets_sha256_json"]):
            raise ValueError("intraday-shadow decision intent target hash drifted")
        return normalized

    def _latest_intent(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM decision_intents ORDER BY trade_date DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        intent = dict(row)
        self._intent_ranked(intent)
        return intent

    def _verify_pending_intent(
        self,
        intent: Mapping[str, Any],
        *,
        require_portfolio_match: bool,
    ) -> dict[str, list[dict[str, Any]]]:
        frozen_at = datetime.fromisoformat(str(intent["frozen_at"]))
        if frozen_at.tzinfo is None:
            raise ValueError("intraday-shadow decision intent timestamp lacks timezone")
        record, snapshot = self._snapshot_at(
            str(intent["trade_date"]),
            self.declaration["timing"]["decisionSlot"],
            available_by=frozen_at.astimezone(SHANGHAI_TZ),
        )
        if (
            record["recordSha256"] != intent["decision_record_sha256"]
            or snapshot["contentSha256"] != intent["decision_snapshot_sha256"]
            or str(_resolve(record["snapshot"]["file"]))
            != intent["decision_snapshot_path"]
        ):
            raise ValueError("intraday-shadow frozen decision snapshot drifted")
        self._quality_quote_map(snapshot, label="decision")
        ranked = _rank_signals(
            snapshot,
            top_k=int(self.declaration["strategy"]["topK"]),
        )
        ranked_contract = self._signal_contract(ranked)
        if _canonical_json(ranked_contract) != str(intent["ranked_json"]):
            raise ValueError("intraday-shadow execution targets differ from frozen intent")
        self._intent_ranked(intent)
        if require_portfolio_match:
            with self._connect() as connection:
                portfolio_sha = self._portfolio_sha256(connection)
            if portfolio_sha != intent["portfolio_sha256"]:
                raise ValueError("intraday-shadow portfolio changed after decision intent")
        return ranked_contract

    def freeze_decision_intent(
        self,
        trade_date: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        parsed = date.fromisoformat(trade_date).isoformat()
        if parsed < str(self.declaration["firstEligibleSignalDate"]):
            raise ValueError(
                "intraday-shadow forbids pre-declaration or calibration-day decisions"
            )
        local_now = (now or datetime.now(SHANGHAI_TZ)).astimezone(SHANGHAI_TZ)
        after = datetime.combine(
            date.fromisoformat(parsed),
            _parse_clock(
                self.causality["decisionFreezeAfter"], "decisionFreezeAfter"
            ),
            SHANGHAI_TZ,
        )
        deadline = datetime.combine(
            date.fromisoformat(parsed),
            _parse_clock(
                self.causality["decisionFreezeDeadline"],
                "decisionFreezeDeadline",
            ),
            SHANGHAI_TZ,
        )
        if local_now < after:
            raise ValueError("intraday-shadow decision cannot freeze before its slot")
        if local_now > deadline or local_now.date().isoformat() != parsed:
            raise ValueError(
                "intraday-shadow decision freeze deadline passed; backfill is forbidden"
            )
        state = self.get_state()
        if state["status"] in {"RISK_STOPPED", "MISSED_SESSION"}:
            raise ValueError(
                f"intraday-shadow run is {state['status']}; new decisions are forbidden"
            )

        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM decision_intents WHERE trade_date = ?", (parsed,)
            ).fetchone()
        if existing is not None:
            self._verify_pending_intent(
                dict(existing),
                require_portfolio_match=str(existing["status"]) == PENDING_INTENT,
            )
            return self.get_state()

        record, snapshot = self._snapshot_at(
            parsed,
            self.declaration["timing"]["decisionSlot"],
            available_by=local_now,
        )
        self._quality_quote_map(snapshot, label="decision")
        ranked = _rank_signals(
            snapshot,
            top_k=int(self.declaration["strategy"]["topK"]),
        )
        ranked_contract = self._signal_contract(ranked)
        ranked_json = _canonical_json(ranked_contract)
        target_hashes = self._targets_sha256(ranked_contract)

        with self._operation_lock(), self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM signal_sessions WHERE trade_date = ?", (parsed,)
            ).fetchone():
                raise ValueError("intraday-shadow session already exists for decision date")
            if connection.execute(
                "SELECT 1 FROM missed_sessions WHERE trade_date = ?", (parsed,)
            ).fetchone():
                raise ValueError("intraday-shadow decision date was already missed")
            pending_prior = connection.execute(
                "SELECT trade_date FROM signal_sessions WHERE trade_date < ? "
                "AND status != 'FINALIZED' ORDER BY trade_date LIMIT 1",
                (parsed,),
            ).fetchone()
            if pending_prior is not None:
                raise ValueError(
                    "intraday-shadow prior session is not finalized before decision"
                )
            portfolio_sha = self._portfolio_sha256(connection)
            connection.execute(
                "INSERT INTO decision_intents(trade_date, status, "
                "decision_record_sha256, decision_snapshot_sha256, "
                "decision_snapshot_path, ranked_json, targets_sha256_json, "
                "portfolio_sha256, frozen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    parsed,
                    PENDING_INTENT,
                    record["recordSha256"],
                    snapshot["contentSha256"],
                    str(_resolve(record["snapshot"]["file"])),
                    ranked_json,
                    _canonical_json(target_hashes),
                    portfolio_sha,
                    local_now.isoformat(),
                ),
            )
            self._append_audit_row(
                connection,
                "DECISION_INTENT_FROZEN_BEFORE_EXECUTION",
                {
                    "tradeDate": parsed,
                    "decisionRecordSha256": record["recordSha256"],
                    "decisionSnapshotSha256": snapshot["contentSha256"],
                    "targetsSha256": target_hashes,
                    "portfolioSha256": portfolio_sha,
                    "executionSnapshotAvailable": False,
                },
                local_now,
            )
        return self.get_state()

    def _reconcile_execution_binding(
        self,
        trade_date: str,
        *,
        now: datetime,
    ) -> bool:
        with self._operation_lock(), self._connect() as connection:
            intent_row = connection.execute(
                "SELECT * FROM decision_intents WHERE trade_date = ?", (trade_date,)
            ).fetchone()
            session = connection.execute(
                "SELECT * FROM signal_sessions WHERE trade_date = ?", (trade_date,)
            ).fetchone()
            if intent_row is None or session is None:
                return False
            intent = dict(intent_row)
            ranked = self._intent_ranked(intent)
            if (
                session["decision_record_sha256"]
                != intent["decision_record_sha256"]
                or session["decision_snapshot_sha256"]
                != intent["decision_snapshot_sha256"]
            ):
                raise ValueError(
                    "intraday-shadow bound session differs from decision intent"
                )
            stored = {
                arm: [
                    dict(row)
                    for row in connection.execute(
                        "SELECT rank, symbol, score, raw_return AS rawReturn, "
                        "turnover_percent AS turnoverPercent FROM signals "
                        "WHERE trade_date = ? AND arm = ? ORDER BY rank",
                        (trade_date, arm),
                    )
                ]
                for arm in ARMS
            }
            if _canonical_json(stored) != _canonical_json(ranked):
                raise ValueError(
                    "intraday-shadow stored signals differ from decision intent"
                )
            if intent["status"] == BOUND_INTENT:
                return False
            if intent["status"] != PENDING_INTENT:
                raise ValueError("intraday-shadow decision intent status is invalid")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE decision_intents SET status = ?, execution_bound_at = ? "
                "WHERE trade_date = ?",
                (BOUND_INTENT, now.isoformat(), trade_date),
            )
            self._append_audit_row(
                connection,
                "EXECUTION_PRICE_BOUND_TO_PRIOR_DECISION_INTENT",
                {
                    "tradeDate": trade_date,
                    "executionRecordSha256": session["execution_record_sha256"],
                    "executionSnapshotSha256": session["execution_snapshot_sha256"],
                    "decisionFrozenAt": intent["frozen_at"],
                },
                now,
            )
            return True

    def _reconcile_all_execution_bindings(self, *, now: datetime) -> list[str]:
        with self._connect() as connection:
            dates = [
                str(row[0])
                for row in connection.execute(
                    "SELECT decision_intents.trade_date FROM decision_intents "
                    "JOIN signal_sessions USING(trade_date) "
                    "WHERE decision_intents.status = ? ORDER BY trade_date",
                    (PENDING_INTENT,),
                )
            ]
        return [
            trade_date
            for trade_date in dates
            if self._reconcile_execution_binding(trade_date, now=now)
        ]

    def freeze_session(
        self,
        trade_date: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        parsed = date.fromisoformat(trade_date).isoformat()
        local_now = (now or datetime.now(SHANGHAI_TZ)).astimezone(SHANGHAI_TZ)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM decision_intents WHERE trade_date = ?", (parsed,)
            ).fetchone()
            session = connection.execute(
                "SELECT 1 FROM signal_sessions WHERE trade_date = ?", (parsed,)
            ).fetchone()
        if row is None:
            raise ValueError(
                "intraday-shadow execution cannot bind without a prior decision intent"
            )
        intent = dict(row)
        if session is not None:
            self._reconcile_execution_binding(parsed, now=local_now)
            return self.get_state()
        self._verify_pending_intent(intent, require_portfolio_match=True)
        super().freeze_session(parsed, now=local_now)
        self._reconcile_execution_binding(parsed, now=local_now)
        return self.get_state()

    def _record_missed_phase(
        self,
        trade_date: str,
        *,
        reason: str,
        now: datetime,
    ) -> bool:
        with self._operation_lock(), self._connect() as connection:
            if connection.execute(
                "SELECT 1 FROM signal_sessions WHERE trade_date = ?", (trade_date,)
            ).fetchone():
                return False
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "INSERT OR IGNORE INTO missed_sessions(trade_date, reason, recorded_at) "
                "VALUES (?, ?, ?)",
                (trade_date, reason, now.isoformat()),
            )
            if not cursor.rowcount:
                return False
            self._append_audit_row(
                connection,
                "CAUSAL_PHASE_MISSED_NO_BACKFILL",
                {"tradeDate": trade_date, "reason": reason},
                now,
            )
            return True

    def get_state(self) -> dict[str, Any]:
        state = super().get_state()
        with self._connect() as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM decision_intents ORDER BY trade_date"
                )
            ]
        for row in rows:
            self._intent_ranked(row)
        latest = rows[-1] if rows else None
        missed_dates = {
            str(row.get("trade_date") or "")
            for row in state["pairedEvidence"]["missedSessions"]
        }
        pending = [
            row
            for row in rows
            if row["status"] == PENDING_INTENT
            and str(row["trade_date"]) not in missed_dates
        ]
        if pending and state["status"] not in {"RISK_STOPPED", "MISSED_SESSION"}:
            pending_date = str(pending[-1]["trade_date"])
            latest_session_date = str(
                (state.get("latestSession") or {}).get("trade_date") or ""
            )
            if pending_date != latest_session_date:
                state["status"] = "DECISION_FROZEN_AWAITING_EXECUTION"
        state["causality"] = dict(self.causality)
        state["protocol"].update(
            {
                "decisionFreezeAfter": self.causality["decisionFreezeAfter"],
                "decisionFreezeDeadline": self.causality[
                    "decisionFreezeDeadline"
                ],
                "executionBindingAfter": self.causality["executionBindingAfter"],
                "executionBindingDeadline": self.causality[
                    "executionBindingDeadline"
                ],
            }
        )
        state["pairedEvidence"].update(
            {
                "decisionIntentSessions": len(rows),
                "pendingDecisionSessions": len(pending),
            }
        )
        state["latestDecisionIntent"] = (
            {
                "tradeDate": latest["trade_date"],
                "status": latest["status"],
                "decisionSnapshotSha256": latest["decision_snapshot_sha256"],
                "targetsSha256": json.loads(latest["targets_sha256_json"]),
                "frozenAt": latest["frozen_at"],
                "executionBoundAt": latest["execution_bound_at"],
            }
            if latest
            else None
        )
        return state

    def run_cycle(
        self,
        *,
        poll_seconds: int,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        local_now = (now or datetime.now(SHANGHAI_TZ)).astimezone(SHANGHAI_TZ)
        previous = self.read_automation_status() or {}
        actions: list[str] = []
        try:
            reconciled = self._reconcile_all_execution_bindings(now=local_now)
            actions.extend(f"EXECUTION_BOUND:{value}" for value in reconciled)
            settled = self.settle_ready_sessions(now=local_now)
            actions.extend(f"FINALIZED:{value}" for value in settled)
            today = local_now.date().isoformat()
            root_provider = _resolve(self.declaration["providerRoot"]["path"])
            trading_dates = {
                line.strip()
                for line in (root_provider / "calendars" / "day_future.txt")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            }
            with self._connect() as connection:
                sessions = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT trade_date FROM signal_sessions"
                    )
                }
                missed = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT trade_date FROM missed_sessions"
                    )
                }
                intents = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT trade_date FROM decision_intents"
                    )
                }
            if today >= str(self.declaration["firstEligibleSignalDate"]):
                for prior in sorted(
                    value
                    for value in trading_dates
                    if str(self.declaration["firstEligibleSignalDate"])
                    <= value
                    < today
                    and value not in sessions
                    and value not in missed
                ):
                    reason = (
                        "EXECUTION_BINDING_DEADLINE_PASSED"
                        if prior in intents
                        else "DECISION_INTENT_DEADLINE_PASSED"
                    )
                    if self._record_missed_phase(
                        prior, reason=reason, now=local_now
                    ):
                        actions.append(f"MISSED:{prior}:{reason}")

            state = self.get_state()
            if (
                today in trading_dates
                and today >= str(self.declaration["firstEligibleSignalDate"])
                and state["status"] not in {"RISK_STOPPED", "MISSED_SESSION"}
            ):
                local_clock = local_now.time().replace(tzinfo=None)
                decision_after = _parse_clock(
                    self.causality["decisionFreezeAfter"], "decisionFreezeAfter"
                )
                decision_deadline = _parse_clock(
                    self.causality["decisionFreezeDeadline"],
                    "decisionFreezeDeadline",
                )
                execution_after = _parse_clock(
                    self.causality["executionBindingAfter"],
                    "executionBindingAfter",
                )
                execution_deadline = _parse_clock(
                    self.causality["executionBindingDeadline"],
                    "executionBindingDeadline",
                )
                has_session = today in sessions
                has_intent = today in intents
                if (
                    not has_session
                    and not has_intent
                    and decision_after <= local_clock <= decision_deadline
                ):
                    if self._slot_record_exists(
                        today, self.declaration["timing"]["decisionSlot"]
                    ):
                        self.freeze_decision_intent(today, now=local_now)
                        actions.append(f"DECISION_FROZEN:{today}")
                        has_intent = True
                    else:
                        actions.append(f"WAITING_DECISION_SNAPSHOT:{today}")
                elif (
                    not has_session
                    and not has_intent
                    and local_clock > decision_deadline
                ):
                    reason = "DECISION_INTENT_DEADLINE_PASSED"
                    if self._record_missed_phase(
                        today, reason=reason, now=local_now
                    ):
                        actions.append(f"MISSED:{today}:{reason}")
                if (
                    not has_session
                    and has_intent
                    and execution_after <= local_clock <= execution_deadline
                ):
                    if self._slot_record_exists(
                        today, self.declaration["timing"]["executionSlot"]
                    ):
                        self.freeze_session(today, now=local_now)
                        actions.append(f"EXECUTION_BOUND:{today}")
                    else:
                        actions.append(f"WAITING_EXECUTION_SNAPSHOT:{today}")
                elif (
                    not has_session
                    and has_intent
                    and local_clock > execution_deadline
                ):
                    reason = "EXECUTION_BINDING_DEADLINE_PASSED"
                    if self._record_missed_phase(
                        today, reason=reason, now=local_now
                    ):
                        actions.append(f"MISSED:{today}:{reason}")

            checkpoint = self.ensure_evidence_checkpoint(now=local_now)
            state = self.get_state()
            payload = {
                "version": STATUS_VERSION,
                "runId": self.run_id,
                "enabled": True,
                "status": state["status"],
                "lastCheckedAt": local_now.isoformat(),
                "lastSuccessAt": local_now.isoformat(),
                "nextCheckAt": (
                    local_now + timedelta(seconds=poll_seconds)
                ).isoformat(),
                "pollSeconds": poll_seconds,
                "consecutiveErrors": 0,
                "lastError": None,
                "actions": actions,
                "state": state,
                "evidenceCheckpoint": checkpoint,
            }
            _atomic_json_write(self.status_path, payload)
            return payload
        except Exception as exc:
            payload = {
                "version": STATUS_VERSION,
                "runId": self.run_id,
                "enabled": True,
                "status": "ERROR",
                "lastCheckedAt": local_now.isoformat(),
                "lastSuccessAt": previous.get("lastSuccessAt"),
                "nextCheckAt": (
                    local_now + timedelta(seconds=poll_seconds)
                ).isoformat(),
                "pollSeconds": poll_seconds,
                "consecutiveErrors": int(previous.get("consecutiveErrors") or 0)
                + 1,
                "lastError": str(exc)[:1000],
                "actions": actions,
                "state": previous.get("state"),
                "evidenceCheckpoint": previous.get("evidenceCheckpoint"),
            }
            _atomic_json_write(self.status_path, payload)
            raise
