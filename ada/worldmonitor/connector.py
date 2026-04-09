"""WorldMonitor data connector — background polling + alert detection.

Runs as an async background service inside Ada. Polls WorldMonitor endpoints
on configurable intervals, detects threshold-crossing events, and pushes
alerts into Ada's event system for voice notification.

Architecture:
  Connector (polling loops) → Alert Detector → Ada Event Bus → Voice/Noteboard
"""

import asyncio
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ada.config import WorldMonitorConfig
from .client import WorldMonitorClient, WMResponse

log = logging.getLogger("ada.worldmonitor.connector")


@dataclass
class WMAlert:
    """An actionable alert detected from WorldMonitor data."""
    category: str           # intelligence, market, conflict, cyber, etc.
    severity: str           # critical, high, medium, low
    title: str              # Short headline
    detail: str             # Longer description for voice/display
    source_service: str     # Which WM service triggered it
    data: Any = None        # Raw data payload
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class WorldMonitorConnector:
    """Background service that polls WorldMonitor and generates alerts."""

    def __init__(
        self,
        config: WorldMonitorConfig,
        on_alert: Optional[Callable] = None,
    ):
        self._config = config
        self._client = WorldMonitorClient(config)
        self._on_alert = on_alert  # async callback(alert: WMAlert)
        self._running = False
        self._tasks: list[asyncio.Task] = []

        # State tracking for delta detection
        self._last_risk_scores: dict[str, float] = {}
        self._last_fear_greed: float | None = None
        self._seen_conflict_ids: set[str] = set()
        self._seen_cyber_ids: set[str] = set()

    async def start(self):
        """Start all polling loops."""
        if not self._config.enabled:
            log.info("WorldMonitor connector disabled")
            return

        self._running = True
        log.info(f"WorldMonitor connector starting — base: {self._client._base}")

        # Check connectivity
        test = await self._client.get_risk_scores()
        if test.error == "connection_refused":
            log.warning("WorldMonitor not reachable — connector will retry in background")
        elif test.error:
            log.warning(f"WorldMonitor test failed: {test.error}")
        else:
            log.info("WorldMonitor connected OK")

        # Launch polling loops
        self._tasks = [
            asyncio.create_task(self._poll_intelligence(), name="wm-intelligence"),
            asyncio.create_task(self._poll_markets(), name="wm-markets"),
            asyncio.create_task(self._poll_conflict(), name="wm-conflict"),
            asyncio.create_task(self._poll_news(), name="wm-news"),
        ]

    async def stop(self):
        """Stop all polling loops."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._client.close()
        log.info("WorldMonitor connector stopped")

    async def _emit_alert(self, alert: WMAlert):
        """Push an alert to Ada's event system."""
        log.info(f"[WM ALERT] [{alert.severity}] {alert.title}")
        if self._on_alert:
            try:
                await self._on_alert(alert)
            except Exception as e:
                log.error(f"Alert callback failed: {e}")

    # ── Polling Loops ────────────────────────────────────────────

    async def _poll_intelligence(self):
        """Poll risk scores and signals."""
        while self._running:
            try:
                # Risk scores for watched countries
                for cc in self._config.watched_countries:
                    resp = await self._client.get_risk_scores(country=cc)
                    if resp.data and not resp.error:
                        self._check_risk_alert(cc, resp.data)

                # Cross-source signals
                resp = await self._client.get_signals()
                if resp.data and not resp.error:
                    self._check_signal_alerts(resp.data)

            except Exception as e:
                log.error(f"Intelligence poll failed: {e}")

            await asyncio.sleep(self._config.intelligence_poll_sec)

    async def _poll_markets(self):
        """Poll market data for significant moves."""
        while self._running:
            try:
                resp = await self._client.get_fear_greed()
                if resp.data and not resp.error:
                    self._check_fear_greed_alert(resp.data)

                resp = await self._client.get_sector_summary()
                if resp.data and not resp.error:
                    self._check_market_move_alerts(resp.data)

            except Exception as e:
                log.error(f"Market poll failed: {e}")

            await asyncio.sleep(self._config.market_poll_sec)

    async def _poll_conflict(self):
        """Poll conflict events for new incidents."""
        while self._running:
            try:
                resp = await self._client.get_conflict_events()
                if resp.data and not resp.error:
                    self._check_conflict_alerts(resp.data)

            except Exception as e:
                log.error(f"Conflict poll failed: {e}")

            await asyncio.sleep(self._config.conflict_poll_sec)

    async def _poll_news(self):
        """Poll news digests."""
        while self._running:
            try:
                resp = await self._client.get_news_digest()
                if resp.data and not resp.error:
                    # News doesn't generate alerts by default — stored for briefings
                    self._latest_news = resp.data

            except Exception as e:
                log.error(f"News poll failed: {e}")

            await asyncio.sleep(self._config.news_poll_sec)

    # ── Alert Detection ──────────────────────────────────────────

    def _check_risk_alert(self, country: str, data: Any):
        """Check if a country's risk score crossed the alert threshold."""
        score = None
        # Data comes as {"ciiScores": [...], "strategicRisks": [...]}
        if isinstance(data, dict):
            scores = data.get("ciiScores", [])
            for s in scores:
                if isinstance(s, dict) and s.get("region") == country:
                    score = s.get("combinedScore")
                    break
            if score is None:
                score = data.get("combinedScore") or data.get("score")
        if isinstance(data, list) and data:
            for s in data:
                if isinstance(s, dict) and s.get("region") == country:
                    score = s.get("combinedScore")
                    break

        if score is None:
            return

        # combinedScore is 0-100, convert to 0-10 scale for threshold
        score = float(score) / 10.0
        prev = self._last_risk_scores.get(country)
        self._last_risk_scores[country] = score

        if score >= self._config.risk_score_alert_threshold:
            # Only alert on new threshold crossings, not every poll
            if prev is None or prev < self._config.risk_score_alert_threshold:
                severity = "critical" if score >= 8.5 else "high"
                asyncio.create_task(self._emit_alert(WMAlert(
                    category="intelligence",
                    severity=severity,
                    title=f"{country} risk score at {score:.1f}/10",
                    detail=f"Country risk index for {country} has reached {score:.1f} out of 10, "
                           f"crossing the alert threshold of {self._config.risk_score_alert_threshold}.",
                    source_service="intelligence",
                    data=data,
                )))

    def _check_signal_alerts(self, data: Any):
        """Check for escalation signals."""
        if not self._config.escalation_alert:
            return
        if not isinstance(data, (list, dict)):
            return

        signals = data if isinstance(data, list) else data.get("signals", [])
        for signal in signals:
            if not isinstance(signal, dict):
                continue
            level = signal.get("level", "").lower()
            if level in ("critical", "high", "escalation"):
                region = signal.get("region", "unknown")
                desc = signal.get("description", signal.get("summary", "Escalation detected"))
                asyncio.create_task(self._emit_alert(WMAlert(
                    category="intelligence",
                    severity="critical" if level == "critical" else "high",
                    title=f"Escalation signal: {region}",
                    detail=desc,
                    source_service="intelligence",
                    data=signal,
                )))

    def _check_fear_greed_alert(self, data: Any):
        """Check for extreme fear/greed shifts."""
        value = None
        if isinstance(data, dict):
            value = data.get("value") or data.get("score")
        if value is None:
            return

        value = float(value)
        prev = self._last_fear_greed
        self._last_fear_greed = value

        # Alert on extreme fear (<20) or extreme greed (>80)
        if value < 20 and (prev is None or prev >= 20):
            asyncio.create_task(self._emit_alert(WMAlert(
                category="market",
                severity="high",
                title=f"Extreme fear: market index at {value:.0f}",
                detail=f"The fear and greed index has dropped to {value:.0f}, indicating extreme fear in markets.",
                source_service="market",
                data=data,
            )))
        elif value > 80 and (prev is None or prev <= 80):
            asyncio.create_task(self._emit_alert(WMAlert(
                category="market",
                severity="medium",
                title=f"Extreme greed: market index at {value:.0f}",
                detail=f"The fear and greed index has risen to {value:.0f}, indicating extreme greed in markets.",
                source_service="market",
                data=data,
            )))

    def _check_market_move_alerts(self, data: Any):
        """Check for significant market sector moves."""
        if not isinstance(data, (list, dict)):
            return
        sectors = data if isinstance(data, list) else data.get("sectors", [])
        for sector in sectors:
            if not isinstance(sector, dict):
                continue
            change = sector.get("change_pct", 0)
            if abs(float(change)) >= self._config.market_move_pct:
                name = sector.get("name", "Unknown sector")
                asyncio.create_task(self._emit_alert(WMAlert(
                    category="market",
                    severity="high" if abs(float(change)) >= 5.0 else "medium",
                    title=f"{name} moved {change:+.1f}%",
                    detail=f"Market sector {name} has moved {change:+.1f}% — exceeds the {self._config.market_move_pct}% alert threshold.",
                    source_service="market",
                    data=sector,
                )))

    def _check_conflict_alerts(self, data: Any):
        """Check for new conflict events."""
        if not isinstance(data, (list, dict)):
            return
        events = data if isinstance(data, list) else data.get("events", [])
        for event in events:
            if not isinstance(event, dict):
                continue
            eid = event.get("id") or event.get("event_id") or str(hash(str(event)))
            if eid in self._seen_conflict_ids:
                continue
            self._seen_conflict_ids.add(eid)

            # Only alert on significant events
            severity_raw = event.get("severity", "").lower()
            if severity_raw in ("critical", "high"):
                location = event.get("location", event.get("country", "unknown"))
                desc = event.get("description", event.get("summary", "New conflict event"))
                asyncio.create_task(self._emit_alert(WMAlert(
                    category="conflict",
                    severity=severity_raw,
                    title=f"Conflict event: {location}",
                    detail=desc[:300],
                    source_service="conflict",
                    data=event,
                )))

        # Cap seen set to prevent memory growth
        if len(self._seen_conflict_ids) > 5000:
            self._seen_conflict_ids = set(list(self._seen_conflict_ids)[-2500:])

    # ── Briefing Data ────────────────────────────────────────────

    async def get_briefing_data(self) -> dict[str, WMResponse]:
        """Fetch a full world snapshot for daily briefing generation."""
        return await self._client.get_world_snapshot(
            countries=self._config.watched_countries
        )

    @property
    def latest_risk_scores(self) -> dict[str, float]:
        """Current cached risk scores per country."""
        return dict(self._last_risk_scores)
