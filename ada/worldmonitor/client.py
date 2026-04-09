"""WorldMonitor API client.

Async HTTP client for querying WorldMonitor's 31 intelligence services.
Supports both self-hosted (localhost:3000) and public API (worldmonitor.app).

All endpoints return JSON. Pattern: GET /api/{domain}/v1/{rpc}?params
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from ada.config import WorldMonitorConfig

log = logging.getLogger("ada.worldmonitor.client")


@dataclass
class WMResponse:
    """Wrapper for WorldMonitor API responses."""
    service: str
    rpc: str
    data: Any
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    cached: bool = False
    error: Optional[str] = None


class WorldMonitorClient:
    """Async client for WorldMonitor API."""

    def __init__(self, config: WorldMonitorConfig):
        self._config = config
        self._base = config.base_url if config.use_local else config.public_url
        self._headers = {}
        if config.api_key:
            self._headers["X-API-Key"] = config.api_key
        self._client: Optional[httpx.AsyncClient] = None

    async def _ensure_client(self):
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base,
                headers=self._headers,
                timeout=15.0,
            )

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _get(self, service: str, rpc: str, params: dict | None = None) -> WMResponse:
        """Make a GET request to a WorldMonitor service endpoint."""
        await self._ensure_client()
        path = f"/api/{service}/v1/{rpc}"
        try:
            resp = await self._client.get(path, params=params or {})
            resp.raise_for_status()
            return WMResponse(service=service, rpc=rpc, data=resp.json())
        except httpx.HTTPStatusError as e:
            log.warning(f"WM API error {e.response.status_code}: {path}")
            return WMResponse(service=service, rpc=rpc, data=None, error=str(e))
        except httpx.ConnectError:
            log.warning(f"WM unreachable at {self._base} — is WorldMonitor running?")
            return WMResponse(service=service, rpc=rpc, data=None, error="connection_refused")
        except Exception as e:
            log.error(f"WM request failed: {e}")
            return WMResponse(service=service, rpc=rpc, data=None, error=str(e))

    # ── Intelligence ──────────────────────────────────────────────

    async def get_risk_scores(self, country: str | None = None) -> WMResponse:
        """Get Country Intelligence Index risk scores."""
        params = {"country": country} if country else {}
        return await self._get("intelligence", "get-risk-scores", params)

    async def get_signals(self, region: str | None = None) -> WMResponse:
        """Get cross-source escalation/de-escalation signals."""
        params = {"region": region} if region else {}
        return await self._get("intelligence", "list-cross-source-signals", params)

    async def get_country_brief(self, country: str) -> WMResponse:
        """Get AI-generated intelligence brief for a country."""
        return await self._get("intelligence", "get-country-intel-brief", {"country": country})

    async def get_gps_jamming(self) -> WMResponse:
        """Get GPS jamming detection data."""
        return await self._get("intelligence", "list-gps-interference")

    async def get_market_implications(self) -> WMResponse:
        """Get AI-analyzed market implications of geopolitical events."""
        return await self._get("intelligence", "list-market-implications")

    # ── Conflict ──────────────────────────────────────────────────

    async def get_conflict_events(self, region: str | None = None) -> WMResponse:
        """Get ACLED/UCDP conflict events."""
        params = {"region": region} if region else {}
        return await self._get("conflict", "list-acled-events", params)

    async def get_humanitarian_summary(self) -> WMResponse:
        """Get humanitarian situation summaries."""
        return await self._get("conflict", "get-humanitarian-summary")

    # ── Market ────────────────────────────────────────────────────

    async def get_quotes(self, symbols: list[str] | None = None) -> WMResponse:
        """Get stock/commodity/crypto quotes."""
        params = {"symbols": ",".join(symbols)} if symbols else {}
        return await self._get("market", "list-market-quotes", params)

    async def get_fear_greed(self) -> WMResponse:
        """Get fear/greed index."""
        return await self._get("market", "get-fear-greed-index")

    async def get_sector_summary(self) -> WMResponse:
        """Get market sector summaries."""
        return await self._get("market", "get-sector-summary")

    async def get_earnings(self) -> WMResponse:
        """Get upcoming/recent earnings reports."""
        return await self._get("market", "list-earnings-calendar")

    # ── Military ──────────────────────────────────────────────────

    async def get_military_flights(self) -> WMResponse:
        """Get tracked military flight activity."""
        return await self._get("military", "list-military-flights")

    async def get_theater_posture(self, region: str | None = None) -> WMResponse:
        """Get military theater posture assessment."""
        params = {"region": region} if region else {}
        return await self._get("military", "get-theater-posture", params)

    # ── Cyber ─────────────────────────────────────────────────────

    async def get_cyber_threats(self) -> WMResponse:
        """Get cyber threat feed."""
        return await self._get("cyber", "list-cyber-threats")

    # ── Infrastructure ────────────────────────────────────────────

    async def get_outages(self) -> WMResponse:
        """Get internet/infrastructure outages."""
        return await self._get("infrastructure", "list-internet-outages")

    # ── News ──────────────────────────────────────────────────────

    async def get_news_digest(self, category: str | None = None) -> WMResponse:
        """Get AI-synthesized news digest."""
        params = {"category": category} if category else {}
        return await self._get("news", "list-feed-digest", params)

    # ── Climate / Natural ─────────────────────────────────────────

    async def get_climate_anomalies(self) -> WMResponse:
        """Get climate anomalies and disaster alerts."""
        return await self._get("climate", "list-climate-anomalies")

    async def get_earthquakes(self) -> WMResponse:
        """Get recent seismic activity."""
        return await self._get("seismology", "list-earthquakes")

    # ── Supply Chain ──────────────────────────────────────────────

    async def get_chokepoint_status(self) -> WMResponse:
        """Get shipping chokepoint status (Suez, Panama, Hormuz, etc.)."""
        return await self._get("supply-chain", "get-chokepoint-status")

    async def get_shipping_rates(self) -> WMResponse:
        """Get global shipping rate stress indicators."""
        return await self._get("supply-chain", "get-shipping-rates")

    # ── Sanctions ─────────────────────────────────────────────────

    async def get_sanctions_pressure(self, country: str | None = None) -> WMResponse:
        """Get sanctions pressure by country/program."""
        params = {"country": country} if country else {}
        return await self._get("sanctions", "list-sanctions-pressure", params)

    # ── Trade ─────────────────────────────────────────────────────

    async def get_tariffs(self, country: str | None = None) -> WMResponse:
        """Get tariff data and trade barriers."""
        params = {"country": country} if country else {}
        return await self._get("trade", "get-tariff-trends", params)

    # ── Batch queries ─────────────────────────────────────────────

    async def get_world_snapshot(self, countries: list[str] | None = None) -> dict[str, WMResponse]:
        """Get a broad snapshot: risk scores, signals, markets, conflicts, news.

        Used for daily briefings and periodic situation awareness.
        """
        tasks = {
            "risk_scores": self.get_risk_scores(),
            "signals": self.get_signals(),
            "fear_greed": self.get_fear_greed(),
            "conflict": self.get_conflict_events(),
            "news": self.get_news_digest(),
            "cyber": self.get_cyber_threats(),
            "chokepoints": self.get_chokepoint_status(),
        }

        # Add per-country briefs for watched countries
        for cc in (countries or []):
            tasks[f"brief_{cc}"] = self.get_country_brief(cc)

        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        snapshot = {}
        for key, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                log.warning(f"Snapshot query {key} failed: {result}")
                snapshot[key] = WMResponse(service="batch", rpc=key, data=None, error=str(result))
            else:
                snapshot[key] = result

        return snapshot
