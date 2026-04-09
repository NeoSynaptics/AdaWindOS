"""Intelligence briefing generator.

Takes raw WorldMonitor data and produces structured briefings
that Ada can speak aloud or display on the noteboard.

Two modes:
  1. Scheduled briefings (morning/evening) — full world snapshot
  2. Alert briefings — immediate voice notification of threshold crossings
"""

import logging
from datetime import datetime, timezone
from typing import Any

from .client import WMResponse
from .connector import WMAlert

log = logging.getLogger("ada.worldmonitor.briefing")


class BriefingGenerator:
    """Generates voice-ready briefings from WorldMonitor data."""

    def generate_daily_briefing(self, snapshot: dict[str, WMResponse]) -> str:
        """Generate a concise daily intelligence briefing for voice delivery.

        Returns a string that Ada can speak directly — written for spoken cadence,
        not written text. Short sentences, no jargon, clear structure.
        """
        sections = []

        # Opening
        now = datetime.now(timezone.utc)
        hour = now.hour
        greeting = "morning" if hour < 12 else "evening"
        sections.append(f"Good {greeting} Daniel. Here's your world intelligence briefing.")

        # Risk scores
        risk_resp = snapshot.get("risk_scores")
        if risk_resp and risk_resp.data:
            risk_summary = self._summarize_risk_scores(risk_resp.data)
            if risk_summary:
                sections.append(risk_summary)

        # Signals
        signals_resp = snapshot.get("signals")
        if signals_resp and signals_resp.data:
            sig_summary = self._summarize_signals(signals_resp.data)
            if sig_summary:
                sections.append(sig_summary)

        # Markets
        fg_resp = snapshot.get("fear_greed")
        if fg_resp and fg_resp.data:
            market_summary = self._summarize_markets(fg_resp.data)
            if market_summary:
                sections.append(market_summary)

        # Conflicts
        conflict_resp = snapshot.get("conflict")
        if conflict_resp and conflict_resp.data:
            conflict_summary = self._summarize_conflicts(conflict_resp.data)
            if conflict_summary:
                sections.append(conflict_summary)

        # News highlights
        news_resp = snapshot.get("news")
        if news_resp and news_resp.data:
            news_summary = self._summarize_news(news_resp.data)
            if news_summary:
                sections.append(news_summary)

        # Cyber
        cyber_resp = snapshot.get("cyber")
        if cyber_resp and cyber_resp.data:
            cyber_summary = self._summarize_cyber(cyber_resp.data)
            if cyber_summary:
                sections.append(cyber_summary)

        # Country briefs
        country_sections = []
        for key, resp in snapshot.items():
            if key.startswith("brief_") and resp and resp.data:
                cc = key.replace("brief_", "")
                brief = self._summarize_country_brief(cc, resp.data)
                if brief:
                    country_sections.append(brief)
        if country_sections:
            sections.append("Country highlights. " + " ".join(country_sections))

        # Closing
        sections.append("That's your briefing. I'll flag anything urgent as it develops.")

        return " ".join(sections)

    def generate_alert_message(self, alert: WMAlert) -> str:
        """Generate a voice-ready alert message from a WMAlert.

        Short, urgent, actionable. Designed to interrupt what Daniel is doing
        only when it matters.
        """
        severity_word = {
            "critical": "Critical alert.",
            "high": "Alert.",
            "medium": "Heads up.",
            "low": "Quick note.",
        }

        prefix = severity_word.get(alert.severity, "Alert.")
        return f"{prefix} {alert.detail}"

    # ── Section Summarizers ──────────────────────────────────────

    def _summarize_risk_scores(self, data: Any) -> str:
        """Summarize risk scores for voice."""
        scores = []
        if isinstance(data, dict):
            scores = data.get("ciiScores", [])
        elif isinstance(data, list):
            scores = data

        # combinedScore is 0-100, threshold at 70
        high_risk = [s for s in scores if isinstance(s, dict) and float(s.get("combinedScore", 0)) >= 70]
        if high_risk:
            names = ", ".join(s.get("region", "?") for s in high_risk[:5])
            return f"Elevated risk detected in {names}."

        if scores:
            top = sorted(scores, key=lambda s: float(s.get("combinedScore", 0)), reverse=True)[:3]
            names = ", ".join(f"{s.get('region','?')} at {s.get('combinedScore',0)}" for s in top)
            return f"Highest risk regions: {names} out of 100."

        return "Global risk levels are within normal ranges."

    def _summarize_signals(self, data: Any) -> str:
        """Summarize escalation/de-escalation signals."""
        if not isinstance(data, (list, dict)):
            return ""
        signals = data if isinstance(data, list) else data.get("signals", [])
        escalations = [s for s in signals if isinstance(s, dict) and
                       s.get("level", "").lower() in ("critical", "high", "escalation")]
        if escalations:
            count = len(escalations)
            regions = ", ".join(set(s.get("region", "unknown") for s in escalations[:3]))
            return f"{count} escalation signal{'s' if count > 1 else ''} active in {regions}."
        return "No significant escalation signals detected."

    def _summarize_markets(self, data: Any) -> str:
        """Summarize market fear/greed."""
        if isinstance(data, dict):
            value = data.get("compositeScore") or data.get("value") or data.get("score")
            label = data.get("compositeLabel") or data.get("label", "")
            vix = data.get("vix")
            if value:
                parts = [f"Market sentiment is at {float(value):.0f}, classified as {label}."]
                if vix:
                    parts.append(f"VIX is at {float(vix):.1f}.")
                # Sector highlights
                sectors = data.get("sectorPerformance", [])
                movers = [s for s in sectors if isinstance(s, dict) and abs(float(s.get("change1d", 0))) >= 2.0]
                if movers:
                    top = sorted(movers, key=lambda s: abs(float(s.get("change1d", 0))), reverse=True)[:3]
                    moves = ", ".join(f"{s['name']} {s['change1d']:+.1f}%" for s in top)
                    parts.append(f"Notable sector moves: {moves}.")
                return " ".join(parts)
        return ""

    def _summarize_conflicts(self, data: Any) -> str:
        """Summarize conflict events."""
        events = data if isinstance(data, list) else data.get("events", []) if isinstance(data, dict) else []
        if not events:
            return ""
        count = len(events)
        if count > 5:
            return f"{count} conflict events tracked globally in the last period."
        return ""

    def _summarize_news(self, data: Any) -> str:
        """Summarize top news."""
        items = data if isinstance(data, list) else data.get("items", []) if isinstance(data, dict) else []
        if items and isinstance(items[0], dict):
            top = items[0]
            headline = top.get("title") or top.get("headline") or top.get("summary", "")
            if headline:
                return f"Top headline: {headline[:150]}."
        return ""

    def _summarize_cyber(self, data: Any) -> str:
        """Summarize cyber threats."""
        threats = data if isinstance(data, list) else data.get("threats", []) if isinstance(data, dict) else []
        critical = [t for t in threats if isinstance(t, dict) and t.get("severity", "").lower() == "critical"]
        if critical:
            return f"{len(critical)} critical cyber threat{'s' if len(critical) > 1 else ''} detected."
        return ""

    def _summarize_country_brief(self, country_code: str, data: Any) -> str:
        """Summarize a country brief."""
        if isinstance(data, dict):
            summary = data.get("summary") or data.get("brief") or data.get("assessment")
            if summary:
                # Truncate for voice
                return f"{country_code}: {str(summary)[:200]}."
        return ""
