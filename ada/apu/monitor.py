"""Hardware monitor — GPU and RAM tracking via nvidia-smi / pynvml.

Provides real snapshots of VRAM usage, temperature, utilization.
Falls back to mock data when not on Linux or no GPU available.
"""

import asyncio
import logging
import shutil
from dataclasses import dataclass

from .models import GPU, GPUSnapshot, HardwareSnapshot

log = logging.getLogger("ada.apu.monitor")


class HardwareMonitor:
    """Async hardware monitor using pynvml (or mock fallback)."""

    def __init__(self):
        self._nvml_available = False
        self._initialized = False

    async def start(self) -> None:
        """Initialize NVML. Falls back to mock if unavailable."""
        try:
            import pynvml
            pynvml.nvmlInit()
            count = pynvml.nvmlDeviceGetCount()
            self._nvml_available = True
            self._initialized = True
            log.info(f"Hardware monitor started: {count} GPU(s) detected via NVML")
        except ImportError:
            log.warning("pynvml not installed — using mock GPU data. Install with: pip install pynvml")
            self._nvml_available = False
            self._initialized = True
        except Exception as e:
            log.warning(f"NVML init failed: {e} — using mock GPU data")
            self._nvml_available = False
            self._initialized = True

    async def close(self) -> None:
        if self._nvml_available:
            try:
                import pynvml
                pynvml.nvmlShutdown()
            except Exception:
                pass
        self._initialized = False

    async def snapshot(self) -> HardwareSnapshot:
        """Get current hardware state."""
        if not self._initialized:
            await self.start()

        if self._nvml_available:
            return await self._real_snapshot()
        else:
            return self._mock_snapshot()

    async def _real_snapshot(self) -> HardwareSnapshot:
        """Real hardware snapshot via pynvml."""
        import pynvml
        import psutil

        def _collect():
            gpus = []
            count = pynvml.nvmlDeviceGetCount()
            for i in range(min(count, 2)):  # max 2 GPUs
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
                try:
                    temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
                except Exception:
                    temp = 0
                try:
                    util = pynvml.nvmlDeviceGetUtilizationRates(handle)
                    gpu_util = util.gpu
                except Exception:
                    gpu_util = 0

                gpu_enum = GPU.GPU_0 if i == 0 else GPU.GPU_1
                gpus.append(GPUSnapshot(
                    gpu=gpu_enum,
                    total_mb=mem.total // (1024 * 1024),
                    used_mb=mem.used // (1024 * 1024),
                    free_mb=mem.free // (1024 * 1024),
                    temperature_c=temp,
                    utilization_pct=gpu_util,
                ))

            ram = psutil.virtual_memory()
            return HardwareSnapshot(
                gpus=gpus,
                ram_total_mb=ram.total // (1024 * 1024),
                ram_used_mb=ram.used // (1024 * 1024),
                ram_free_mb=ram.available // (1024 * 1024),
            )

        return await asyncio.get_event_loop().run_in_executor(None, _collect)

    def _mock_snapshot(self) -> HardwareSnapshot:
        """Mock snapshot for dev/testing without GPUs."""
        return HardwareSnapshot(
            gpus=[
                GPUSnapshot(gpu=GPU.GPU_0, total_mb=16384, used_mb=0, free_mb=16384),
                GPUSnapshot(gpu=GPU.GPU_1, total_mb=12288, used_mb=0, free_mb=12288),
            ],
            ram_total_mb=131072,
            ram_used_mb=16000,
            ram_free_mb=115072,
        )

    async def get_gpu_free_mb(self, gpu: GPU) -> int:
        """Quick check: how much VRAM is free on a specific GPU."""
        snap = await self.snapshot()
        gpu_snap = snap.gpu(gpu)
        return gpu_snap.free_mb if gpu_snap else 0

    async def is_gpu_available(self, gpu: GPU, needed_mb: int) -> bool:
        """Check if a GPU has enough free VRAM."""
        free = await self.get_gpu_free_mb(gpu)
        return free >= needed_mb

    async def health_check(self) -> dict:
        """Health check for Ada's health monitor."""
        try:
            snap = await self.snapshot()
            return {
                "status": "ok",
                "gpus": [
                    {
                        "gpu": g.gpu.name,
                        "free_mb": g.free_mb,
                        "used_mb": g.used_mb,
                        "temp_c": g.temperature_c,
                        "util_pct": g.utilization_pct,
                    }
                    for g in snap.gpus
                ],
                "ram_free_mb": snap.ram_free_mb,
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}
