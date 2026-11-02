"""Collects CPU, memory, GPU and fan readings into a single sample.

Sources, in order of reliability on this machine:

* CPU load and memory      -- psutil (Windows performance counters)
* GPU load, VRAM, temp     -- nvidia-smi, which ships with the NVIDIA driver
* CPU temp and fan speeds  -- the MSI embedded controller (see msi_ec.py)

The NVIDIA RTX 3050 is the discrete GPU; the Intel Iris Xe iGPU is ignored
because the thing worth watching on this laptop is the dGPU. nvidia-smi reports
fan.speed as [N/A] here -- laptop GPU fans are driven by the EC, not the GPU --
which is why fan data comes from msi_ec instead.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

import psutil

import msi_ec

# Keeps nvidia-smi from flashing a console window on each poll.
_NO_WINDOW = 0x08000000

_SMI_FIELDS = "utilization.gpu,memory.used,memory.total,temperature.gpu"


@dataclass
class Sample:
    cpu_pct: float = 0.0
    cpu_temp: float | None = None

    ram_pct: float = 0.0
    ram_used_gb: float = 0.0
    ram_total_gb: float = 0.0

    gpu_pct: float | None = None
    gpu_temp: float | None = None
    gpu_mem_used_mb: float | None = None
    gpu_mem_total_mb: float | None = None

    fan_values: list[int] | None = None
    fan_unit: str = "RPM"


class SensorReader:
    """Polls every source. Construct and use on one thread (COM is per-thread)."""

    def __init__(self, fan_mode: str = "rpm") -> None:
        self.fan_mode = fan_mode
        self.ec = msi_ec.MsiEc()
        # Resolve to an absolute path: shutil.which() searches the current
        # directory first on Windows, and the widget's cwd is not stable.
        found = shutil.which("nvidia-smi")
        self._smi = os.path.abspath(found) if found else None
        self._gpu_cache: dict[str, float | None] | None = None

        # First psutil call establishes the baseline for percentage deltas.
        psutil.cpu_percent(interval=None)

    # -- individual sources --------------------------------------------------

    def _read_gpu(self) -> dict[str, float | None] | None:
        if not self._smi:
            return None
        try:
            out = subprocess.run(
                [self._smi, f"--query-gpu={_SMI_FIELDS}", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=4,
                creationflags=_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if out.returncode != 0 or not out.stdout.strip():
            return None

        parts = [p.strip() for p in out.stdout.strip().splitlines()[0].split(",")]
        if len(parts) < 4:
            return None

        def num(text: str) -> float | None:
            try:
                return float(text)
            except ValueError:  # nvidia-smi prints [N/A] for unsupported fields
                return None

        return {
            "load": num(parts[0]),
            "mem_used": num(parts[1]),
            "mem_total": num(parts[2]),
            "temp": num(parts[3]),
        }

    # -- public --------------------------------------------------------------

    def read(self, include_slow: bool = True) -> Sample:
        """Read all sources.

        ``include_slow`` gates nvidia-smi and the EC, which cost a few
        milliseconds more than psutil; the widget polls them less often and
        reuses the previous value in between.
        """
        sample = Sample()

        sample.cpu_pct = psutil.cpu_percent(interval=None)
        vm = psutil.virtual_memory()
        sample.ram_pct = vm.percent
        sample.ram_used_gb = (vm.total - vm.available) / 1024**3
        sample.ram_total_gb = vm.total / 1024**3

        if include_slow or self._gpu_cache is None:
            self._gpu_cache = self._read_gpu()
        gpu = self._gpu_cache
        if gpu:
            sample.gpu_pct = gpu["load"]
            sample.gpu_temp = gpu["temp"]
            sample.gpu_mem_used_mb = gpu["mem_used"]
            sample.gpu_mem_total_mb = gpu["mem_total"]

        ec = self.ec.read()
        # nvidia-smi is the better GPU thermometer; fall back to the EC's byte.
        if sample.gpu_temp is None:
            sample.gpu_temp = ec.gpu_temp
        sample.cpu_temp = ec.cpu_temp

        if ec.fans:
            if self.fan_mode == "duty":
                sample.fan_values = [f.duty for f in ec.fans]
                sample.fan_unit = "%"
            else:
                sample.fan_values = [f.rpm for f in ec.fans]
                sample.fan_unit = "RPM"

        return sample

    @property
    def fan_status(self) -> str:
        """Why fan data is missing, for the UI to show instead of a number."""
        if self.ec.available:
            return "ok"
        return self.ec.error or "unavailable"
