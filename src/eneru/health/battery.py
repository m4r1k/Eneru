"""Battery depletion-rate tracking and anomaly detection.

Owns the rolling battery-history file (``self._battery_history_path``) used
to compute depletion rate over a configurable window, plus sustained-reading
confirmation of charge anomalies that survive 3 consecutive polls (filters
firmware jitter from APC / CyberPower / UniFi UPS units).
"""

import time
from collections import deque
from typing import Dict, Optional

from eneru.health import prediction
from eneru.utils import is_numeric

# meta keys for cross-restart battery-health bookkeeping.
_META_NOMINAL_RUNTIME = "battery_nominal_runtime"
_META_REPLACEMENT_PREDICTED = "battery_replacement_predicted_ts"
_META_HEALTH_ALERT_TIER = "battery_health_alert_tier"  # none|warn|critical

_HEALTH_TIER_RANK = {"none": 0, "warn": 1, "critical": 2}


class BatteryMonitorMixin:
    """Mixin: battery depletion-rate calculation, anomaly detection, and the
    v6.1 battery-health score + replacement prediction."""

    def _calculate_depletion_rate(self, current_battery: str) -> float:
        """Calculate battery depletion rate based on history."""
        current_time = int(time.time())

        if not is_numeric(current_battery):
            return 0.0

        # M12: clamp out-of-range firmware readings. A transient negative or
        # >100 charge from flaky firmware would otherwise inflate the depletion
        # rate that feeds the T3 shutdown trigger.
        current_battery_float = max(0.0, min(100.0, float(current_battery)))
        cutoff_time = current_time - self.config.triggers.depletion.window

        self.state.battery_history = deque(
            [(ts, bat) for ts, bat in self.state.battery_history if ts >= cutoff_time],
            maxlen=1000
        )
        self.state.battery_history.append((current_time, current_battery_float))

        # L17 (evaluated, deferred): this file is written every poll but not
        # read back at startup. It's a forensic record today. Reading it back to
        # let depletion-rate survive a daemon restart mid-outage is entangled
        # with the on-battery deque reset in monitor._handle_on_battery (a fresh
        # OB transition clears battery_history), which is the same
        # restart-adoption area as the H3 work -- so a safe cross-restart
        # read-back is a larger change than a Low warrants and is deferred.
        try:
            # with_name(name + '.tmp') preserves the per-UPS suffix on the
            # path (e.g. 'ups-battery-history.ups1') so concurrent writers
            # in multi-UPS mode never share a temp file. with_suffix('.tmp')
            # would replace the per-UPS suffix and race on the rename.
            temp_file = self._battery_history_path.with_name(
                self._battery_history_path.name + '.tmp'
            )
            with open(temp_file, 'w') as f:
                for ts, bat in self.state.battery_history:
                    f.write(f"{ts}:{bat}\n")
            temp_file.replace(self._battery_history_path)
        except Exception as exc:
            # Persisting battery history is best-effort; the in-memory deque
            # is the source of truth and a single failed write doesn't break
            # depletion calculations. Log so silent disk errors are visible.
            self._log_message(
                f"⚠️  Battery history persist failed: {exc}"
            )

        # Historically this required a flat 30 samples. But the deque is first
        # pruned to `depletion.window` seconds, so the most samples that can ever
        # survive is ~window/check_interval. With a slow poll interval (e.g.
        # check_interval=11, window=300 -> ~27 samples) a flat 30 is never
        # reached and the depletion trigger (T3) is silently dead forever.
        # Require min(30, window/check_interval) -- capped by what the window can
        # actually hold so T3 stays armed -- with a floor of 2 (a rate needs two
        # points). The floor must NOT exceed the holdable count, or tiny windows
        # would re-disable T3 (cubic P2); 2 points is the physical minimum, so a
        # window smaller than ~2*check_interval genuinely can't compute a rate.
        try:
            check_interval = max(1, int(self.config.ups.check_interval))
        except (TypeError, ValueError):
            check_interval = 1
        window = self.config.triggers.depletion.window
        min_samples = min(30, max(2, window // check_interval))
        if len(self.state.battery_history) < min_samples:
            return 0.0

        oldest_time, oldest_battery = self.state.battery_history[0]
        time_diff = current_time - oldest_time

        if time_diff > 0:
            battery_diff = oldest_battery - current_battery_float
            rate = (battery_diff / time_diff) * 60
            return round(rate, 2)

        return 0.0

    def _check_battery_anomaly(self, ups_data: Dict[str, str]):
        """Detect abnormal battery charge changes while on line power.

        Catches firmware recalibrations, battery aging events, or hardware
        issues that cause sudden charge drops (e.g., 100% -> 60% in seconds)
        while the UPS is on line power and not discharging.

        Uses sustained-reading confirmation: an anomalous drop must persist
        across 3 consecutive polls before firing.  This filters out transient
        firmware jitter that some UPS units (notably APC, CyberPower, and
        Ubiquiti UniFi UPS) exhibit after an OB -> OL transition, where the first
        few readings may report a wildly incorrect charge that self-corrects
        within a couple of seconds.
        """
        ups_status = ups_data.get('ups.status', '')
        battery_charge_str = ups_data.get('battery.charge', '')

        if not is_numeric(battery_charge_str):
            return

        current_charge = float(battery_charge_str)
        current_time = time.time()

        # Only track anomalies while on line power (OL/CHRG)
        if "OB" in ups_status:
            # On battery -- reset tracking, drops are expected
            self.state.last_battery_charge = current_charge
            self.state.last_battery_charge_time = current_time
            self.state.pending_anomaly_charge = -1.0
            self.state.pending_anomaly_count = 0
            return

        prev_charge = self.state.last_battery_charge
        prev_time = self.state.last_battery_charge_time

        # Update tracking
        self.state.last_battery_charge = current_charge
        self.state.last_battery_charge_time = current_time

        # Skip if not yet initialized
        if prev_charge < 0:
            return

        # Check for significant drop while online
        drop = prev_charge - current_charge
        elapsed = current_time - prev_time if prev_time > 0 else 0

        # Threshold: >20% drop within 120 seconds while on line power.
        # M11: only START a fresh detection when none is pending. A battery that
        # keeps dropping fast every poll previously re-entered here each time,
        # resetting pending_anomaly_count to 1 so the 3-poll confirmation was
        # never reached. With a pending anomaly we fall through to the
        # confirmation branch below, which increments the counter instead.
        if drop > 20 and elapsed < 120 and self.state.pending_anomaly_charge < 0:
            # First detection -- record as pending, wait for confirmation
            self.state.pending_anomaly_charge = current_charge
            self.state.pending_anomaly_prev_charge = prev_charge
            self.state.pending_anomaly_time = current_time
            self.state.pending_anomaly_count = 1
            return

        # Check if a pending anomaly is being confirmed across polls
        if self.state.pending_anomaly_charge >= 0:
            # Charge recovered -- transient jitter, discard the anomaly
            if current_charge > self.state.pending_anomaly_charge + 10:
                self.state.pending_anomaly_charge = -1.0
                self.state.pending_anomaly_count = 0
                return

            # Still low -- increment confirmation counter
            self.state.pending_anomaly_count += 1

            # Need 3 consecutive polls to confirm (filters firmware jitter)
            if self.state.pending_anomaly_count < 3:
                return

            # Re-validate the drop magnitude before notifying. Without this
            # check the confirmation can fire after the charge has crept
            # back up to within a few % of the original (drop < 20),
            # producing a false-alarm "battery dropped X%" message that
            # contradicts the current reading.
            anomaly_prev = self.state.pending_anomaly_prev_charge
            anomaly_drop = anomaly_prev - current_charge
            if anomaly_drop <= 20:
                self.state.pending_anomaly_charge = -1.0
                self.state.pending_anomaly_count = 0
                return

            # Confirmed anomaly (sustained across 3 polls)
            anomaly_elapsed = current_time - self.state.pending_anomaly_time
            self.state.pending_anomaly_charge = -1.0
            self.state.pending_anomaly_count = 0
            # v6.1: feed the battery-health anomaly term.
            self.state.confirmed_anomaly_count += 1

            self._log_message(
                f"⚠️  WARNING: Battery charge dropped from {anomaly_prev:.0f}% to "
                f"{current_charge:.0f}% ({anomaly_drop:.0f}% drop) while on line power. "
                f"Possible firmware recalibration, battery aging, or hardware issue."
            )
            self._send_notification(
                f"⚠️  **Battery Anomaly Detected**\n"
                f"Charge dropped from {anomaly_prev:.0f}% to {current_charge:.0f}% "
                f"({anomaly_drop:.0f}% drop in {anomaly_elapsed:.0f}s) while on line power.\n"
                f"Possible causes: firmware recalibration, battery aging, or hardware issue.",
                self.config.NOTIFY_WARNING,
                category="health",
            )

    # ----- v6.1: battery-health score + replacement prediction -----

    def _resolve_battery_health_config(self):
        """Per-UPS battery_health override if present, else the global config.

        Mirrors the API's per-group nut_control resolution; the override is
        already merged-with-global at parse time, so it is the effective config.
        """
        glob = self.config.battery_health
        try:
            name = self.config.ups.name
            for group in getattr(self.config, "ups_groups", None) or []:
                if (getattr(group.ups, "name", None) == name
                        and getattr(group, "battery_health", None)):
                    return group.battery_health
        except AttributeError as e:
            # A malformed config shape (e.g. ups/name missing) is a real bug, not
            # something to swallow silently — surface it, then fall back to the
            # global config so health tracking still runs.
            self._log_message(
                f"battery-health config resolution failed, using global "
                f"config: {e}")
        return glob

    def _open_store(self):
        """Return the per-UPS stats store only when it is actually open. A store
        created in __init__ but never opened (or already closed) silently no-ops
        get_meta/set_meta and returns empty reads, so every battery-health
        caller must treat it as unavailable — otherwise learning/prediction
        degrades into a silent no-op instead of failing soft on a real store."""
        store = getattr(self, "_stats_store", None)
        return store if store is not None and store.is_open else None

    def _learned_nominal_runtime(self) -> Optional[float]:
        """Read the nominal full-charge runtime learned at a 100%-charge poll
        (persisted in meta so it survives restarts)."""
        store = self._open_store()
        if store is None:
            return None
        raw = store.get_meta(_META_NOMINAL_RUNTIME)
        try:
            return float(raw) if raw else None
        except (TypeError, ValueError):
            return None

    def _maybe_learn_nominal_runtime(self, charge: Optional[float],
                                     runtime_s: Optional[float]) -> None:
        """At full charge, record runtime as the nominal once (if not already
        learned and not configured)."""
        store = self._open_store()
        if store is None or charge is None or runtime_s is None:
            return
        # If the config pins a nominal runtime, _compute_battery_health prefers
        # it anyway — don't pollute meta with an auto-learned value that will
        # never be used (and would resurface if the pin is later removed).
        cfg = self._resolve_battery_health_config()
        if getattr(cfg, "nominal_runtime_seconds", None):
            return
        if charge >= 99.0 and runtime_s > 0 and not self._learned_nominal_runtime():
            store.set_meta(_META_NOMINAL_RUNTIME, str(int(runtime_s)))

    def _battery_runtime_history(self, now: float, days: int = 60):
        """[(ts, runtime_s)] from stored battery_health detail, for the
        capacity trend term."""
        store = self._open_store()
        if store is None:
            return []
        out = []
        for row in store.query_battery_health(int(now - days * 86400), int(now)):
            detail = row.get("detail") or {}
            rt = detail.get("runtime_s")
            if rt is not None:
                out.append((float(row["ts"]), float(rt)))
        return out

    def _compute_battery_health(self, cfg, now: float) -> Dict:
        """Compute the weighted battery-health block. Unknown terms stay
        unavailable (None) -- thin telemetry never yields a confident score."""
        def _num(value):
            try:
                return float(value) if is_numeric(value) else None
            except (TypeError, ValueError):
                return None

        with self.state._lock:
            charge = _num(self.state.latest_battery_charge)
            runtime_s = _num(self.state.latest_runtime)
            anomaly_count = self.state.confirmed_anomaly_count

        self._maybe_learn_nominal_runtime(charge, runtime_s)
        nominal = cfg.nominal_runtime_seconds
        if nominal is None:
            nominal = self._learned_nominal_runtime()

        st = None
        store = self._open_store()
        if store is not None:
            latest = store.latest_self_test()
            st = latest.get("result_enum") if latest else None

        # Fetch enough runtime history to cover the capacity term's span guard:
        # capacity_score needs span >= min_history_days, which has no upper bound
        # in validation, so a min_history_days > 60 would permanently starve the
        # capacity term if we hard-capped the fetch at 60 days.
        min_history_days = cfg.replacement.min_history_days
        terms = prediction.compute_terms(
            current_runtime_s=runtime_s,
            nominal_runtime_s=nominal,
            runtime_history=self._battery_runtime_history(
                now, days=max(60, int(min_history_days) + 7)),
            self_test_result=st,
            anomaly_count=anomaly_count,
            battery_install_date=cfg.battery_install_date,
            expected_life_years=cfg.expected_life_years,
            now=now,
            # Don't infer a capacity trend from too short a window — the same
            # min span the replacement projection requires (avoids a confident
            # zero from a few hours of jitter on a new battery).
            min_history_days=min_history_days,
        )
        score, confidence, available = prediction.composite_score(terms)
        return {
            "score": score,
            "confidence": round(confidence, 3),
            "availableTerms": available,
            "terms": terms,
            "runtime_s": runtime_s,
            "nominalRuntime": nominal,
            "ts": int(now),
        }

    def _update_battery_health_periodic(self, now: Optional[float] = None) -> None:
        """Compute + persist the battery-health score and run replacement
        prediction. Called on the battery_health.update_interval cadence."""
        if now is None:
            now = time.time()
        cfg = self._resolve_battery_health_config()
        if not cfg.enabled:
            # Clear any previously-published block so a reload to disabled does
            # not leave a stale score on the API/MQTT/status surfaces.
            with self.state._lock:
                self.state.latest_battery_health = None
            return
        health = self._compute_battery_health(cfg, now)
        store = self._open_store()
        # Persist FIRST so the prediction trend includes this fresh point.
        if store is not None:
            store.record_battery_health(
                health["score"], health["terms"],
                detail={"confidence": health["confidence"],
                        "runtime_s": health["runtime_s"],
                        "nominalRuntime": health["nominalRuntime"]},
                ts=int(now))
        # Prediction feeds both the published block and the (deduped) warning.
        pred = self._maybe_predict_replacement(cfg, now)
        health["replacementDaysRemaining"] = pred.get("days_remaining")
        health["replacementDue"] = bool(pred.get("due"))
        # Escalating absolute-score alerts (separate from the trend prediction).
        self._maybe_alert_health(cfg, health.get("score"))
        with self.state._lock:
            self.state.latest_battery_health = health

    def _maybe_predict_replacement(self, cfg, now: float) -> Dict:
        """Trend the stored score series; warn once per period if the battery
        is projected to need replacement within the horizon. Returns the
        prediction result dict (used to enrich the status block)."""
        store = self._open_store()
        if store is None:
            return {"due": False, "days_remaining": None}
        rep = cfg.replacement
        rows = store.query_battery_health(
            int(now - 365 * 86400), int(now))
        history = [(float(r["ts"]), float(r["score"]))
                   for r in rows if r.get("score") is not None]
        result = prediction.predict_replacement(
            history,
            threshold_score=rep.threshold_score,
            horizon_days=rep.horizon_days,
            min_history_days=rep.min_history_days,
            now=now,
        )
        if not result["due"]:
            return result
        # Dedup: re-nag at most weekly while the battery stays due. Silencing the
        # warning for the full horizon_days (default 90d) means a battery that is
        # already overdue goes quiet for a quarter — re-warn weekly instead, but
        # never more often than the horizon if that is shorter than a week.
        last_raw = store.get_meta(_META_REPLACEMENT_PREDICTED)
        try:
            last = float(last_raw) if last_raw else 0.0
        except (TypeError, ValueError):
            last = 0.0
        if now - last < min(rep.horizon_days, 7) * 86400:
            return result
        store.set_meta(_META_REPLACEMENT_PREDICTED, str(int(now)))
        days = result.get("days_remaining")
        if not days:
            days_txt = "imminently"
        elif days < 1:
            days_txt = "<1 day"
        else:
            days_txt = f"~{days:.0f} days"
        self._log_message(
            f"🔋 Battery replacement predicted: health trending below "
            f"{rep.threshold_score:.0f} in {days_txt}.")
        self._send_notification(
            f"🔋 **Battery Replacement Predicted**\n"
            f"The battery-health score is projected to cross "
            f"{rep.threshold_score:.0f} in {days_txt}. Plan a replacement.",
            self.config.NOTIFY_WARNING,
            category="health",
        )
        # store is guaranteed non-None here (early return above).
        store.log_event(
            "BATTERY_REPLACEMENT_PREDICTED",
            f"health trend crosses {rep.threshold_score:.0f} in {days_txt}")
        return result

    def _maybe_alert_health(self, cfg, score) -> None:
        """Escalating, deduped absolute-score alerts (separate from the
        trend-based prediction). Fires once when the score first drops below a
        configured tier and re-arms when it recovers above it; a drop straight
        to / further into 'critical' escalates. A de-escalation only re-arms."""
        if score is None:
            return
        store = self._open_store()
        if store is None:
            return
        warn, crit = cfg.warn_score, cfg.critical_score
        if crit is not None and score < crit:
            tier = "critical"
        elif warn is not None and score < warn:
            tier = "warn"
        else:
            tier = "none"
        prev = store.get_meta(_META_HEALTH_ALERT_TIER) or "none"
        if tier == prev:
            return                                   # unchanged -> dedup
        store.set_meta(_META_HEALTH_ALERT_TIER, tier)
        # Only ANNOUNCE on escalation; recovery / de-escalation just re-arms.
        if _HEALTH_TIER_RANK[tier] <= _HEALTH_TIER_RANK.get(prev, 0):
            return
        if tier == "critical":
            self._log_message(
                f"🔋 Battery health CRITICAL: score {score:.0f} (< {crit:.0f}).")
            self._send_notification(
                f"🔋 **Battery Health CRITICAL**\n"
                f"The battery-health score is {score:.0f} (below {crit:.0f}). "
                f"Replace the battery now.",
                self.config.NOTIFY_FAILURE, category="health")
            store.log_event("BATTERY_HEALTH_CRITICAL",
                            f"score {score:.0f} < {crit:.0f}")
        else:
            self._log_message(
                f"🔋 Battery health low: score {score:.0f} (< {warn:.0f}).")
            self._send_notification(
                f"🔋 **Battery Health Warning**\n"
                f"The battery-health score is {score:.0f} (below {warn:.0f}). "
                f"Plan to replace the battery.",
                self.config.NOTIFY_WARNING, category="health")
            store.log_event("BATTERY_HEALTH_WARNING",
                            f"score {score:.0f} < {warn:.0f}")
