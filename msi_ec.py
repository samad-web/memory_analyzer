r"""MSI notebook embedded-controller sensors via the MSI_ACPI WMI interface.

MSI laptops publish an ACPI-WMI object at ``root\WMI:MSI_ACPI`` whose ``Get_*``
methods exchange a 32-byte package (the ``Package_32`` class). Reading it needs
administrator rights -- unelevated callers get "Access denied".

This is the only fan source that works on this machine: LibreHardwareMonitor
needs the WinRing0 kernel driver, which Windows 11's vulnerable-driver blocklist
refuses to load, and ``Win32_Fan`` reports three nameless cooling devices with
no RPM values.

Layout verified on MS-1584 (EC firmware "1584EMS1.107", built 03/18/2022):

    Get_Temperature, index 0 -> [status, cpu_temp_c, gpu_temp_c, ...]
    Get_Fan,         index 0 -> [status, cpu_hi, cpu_lo, gpu_hi, gpu_lo, ...]
    Get_Fan,         index 1 -> CPU fan curve duty points (%)
    Get_Fan,         index 2 -> GPU fan curve duty points (%)
    Get_Temperature, index 1 -> CPU fan curve temperature thresholds (C)
    Get_Temperature, index 2 -> GPU fan curve temperature thresholds (C)

The index-0 fan bytes are a pair of 16-bit big-endian values. MSI encodes live
fan speed as a divisor -- ``rpm = 478000 / raw`` -- which is what the Linux
``msi-ec`` driver uses for this generation. On a laptop whose EC is already
holding the fans high the same byte can also be read as a duty percentage, so
``FanReading`` carries the raw value and both interpretations; the widget picks
one via the ``fan_mode`` config key.

Only ``Get_*`` methods are ever called. The matching ``Set_*`` methods would
write EC state (fan curves, thermal profile) and are deliberately never touched.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass, field

# MSI's divisor for converting a raw EC fan value into RPM.
RPM_DIVISOR = 478000

_WMI_NAMESPACE = "winmgmts://./root/WMI"
_PACKAGE_CLASS = "Package_32"
_ACPI_CLASS = "MSI_ACPI"


def is_elevated() -> bool:
    """True when the current process has an administrator token."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


@dataclass
class FanReading:
    """One fan's live value, with both plausible interpretations of the byte."""

    raw: int
    rpm: int
    duty: int


@dataclass
class EcSample:
    cpu_temp: float | None = None
    gpu_temp: float | None = None
    fans: list[FanReading] = field(default_factory=list)


class MsiEc:
    """Reads temperatures and fan speeds from the MSI ACPI-WMI interface.

    Construct it on the thread that will poll it -- COM is initialised per
    thread. ``available`` is False when the interface is missing or the process
    is not elevated, and every read then returns an empty sample.
    """

    def __init__(self) -> None:
        self.available = False
        self.error: str | None = None
        self._acpi = None
        self._wmi = None
        self._fan_curve: list[int] = []
        self._temp_curve: list[int] = []
        self.ec_version: str | None = None

        if not is_elevated():
            self.error = "not running as administrator"
            return
        try:
            self._connect()
            self.available = True
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI, not raised
            self.error = str(exc).strip() or exc.__class__.__name__

    # -- setup ---------------------------------------------------------------

    def _connect(self) -> None:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        self._wmi = win32com.client.GetObject(_WMI_NAMESPACE)
        instances = list(self._wmi.ExecQuery(f"SELECT * FROM {_ACPI_CLASS}"))
        if not instances:
            raise RuntimeError(f"{_ACPI_CLASS} has no instances")
        self._acpi = instances[0]

        # Cache the things that never change while the machine is running.
        self.ec_version = self._read_ec_version()
        self._fan_curve = self._call("Get_Fan", 1)[1:9]
        self._temp_curve = self._call("Get_Temperature", 1)[1:9]

    # -- low level -----------------------------------------------------------

    def _call(self, method: str, index: int = 0) -> list[int]:
        """Invoke a Get_* method with ``index`` in byte 0, returning 32 bytes."""
        package = self._wmi.Get(_PACKAGE_CLASS).SpawnInstance_()
        payload = [0] * 32
        payload[0] = index
        package.Bytes = payload

        in_params = self._acpi.Methods_(method).InParameters.SpawnInstance_()
        in_params.Data = package
        result = self._acpi.ExecMethod_(method, in_params)
        return [int(b) for b in result.Data.Bytes]

    def _read_ec_version(self) -> str | None:
        """Get_EC takes no input and returns the EC firmware banner as ASCII."""
        try:
            out = self._acpi.ExecMethod_("Get_EC")
            raw = [int(b) for b in out.Data.Bytes]
        except Exception:
            return None
        text = bytes(b for b in raw[2:] if 32 <= b < 127).decode("ascii", "ignore")
        return text.strip() or None

    @staticmethod
    def _fan(hi: int, lo: int) -> FanReading:
        raw = (hi << 8) | lo
        rpm = round(RPM_DIVISOR / raw) if raw else 0
        return FanReading(raw=raw, rpm=rpm, duty=min(raw, 100))

    # -- public --------------------------------------------------------------

    def read(self) -> EcSample:
        if not self.available:
            return EcSample()
        try:
            temps = self._call("Get_Temperature", 0)
            fans = self._call("Get_Fan", 0)
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc).strip() or exc.__class__.__name__
            self.available = False
            return EcSample()

        return EcSample(
            cpu_temp=_plausible_temp(temps[1]),
            gpu_temp=_plausible_temp(temps[2]),
            fans=[self._fan(fans[1], fans[2]), self._fan(fans[3], fans[4])],
        )

    def describe_curve(self) -> str:
        """The EC's fan curve, for the README / tooltip.

        The EC returns the seven duty points in order but the matching
        temperature thresholds rotated, so the thresholds are sorted back into
        ascending order before pairing. Both curves are monotonic, which makes
        that unambiguous.
        """
        if not self._fan_curve or not self._temp_curve:
            return "fan curve unavailable"
        duties = self._fan_curve[1:8]
        temps = sorted(self._temp_curve[1:8])
        pairs = [f"{t}C->{d}%" for t, d in zip(temps, duties) if t and d]
        return ", ".join(pairs)


def _plausible_temp(value: int) -> float | None:
    """EC temperature bytes read 0 or 0xFF when a sensor is not populated."""
    return float(value) if 0 < value < 125 else None
