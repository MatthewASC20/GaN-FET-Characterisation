"""Hardware self-test and connectivity diagnostic routines for the GaN FET rig."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional

from gan_fet.settings import SCOPE_INSTRUMENT_KEY, Settings

log = logging.getLogger(__name__)


@dataclass
class DiagnosticResult:
    instrument_name: str
    ip: str
    port: int
    online: bool
    idn_string: Optional[str] = None
    response_time_ms: float = 0.0
    error_msg: Optional[str] = None


@dataclass
class DiagnosticReport:
    results: List[DiagnosticResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return len(self.results) > 0 and all(r.online for r in self.results)

    def summary(self) -> str:
        lines = [
            "==================================================",
            "        GaN FET Rig Hardware Self-Test            ",
            "==================================================",
        ]
        for r in self.results:
            status = "PASS [ONLINE]" if r.online else "FAIL [OFFLINE]"
            lines.append(f"[{status}] {r.instrument_name} ({r.ip}:{r.port})")
            if r.online:
                lines.append(f"  IDN: {r.idn_string}")
                lines.append(f"  Latency: {r.response_time_ms:.1f} ms")
            else:
                lines.append(f"  Error: {r.error_msg}")
            lines.append("--------------------------------------------------")
        lines.append(f"Overall Status: {'ALL PASSED' if self.all_passed else 'DIAGNOSTICS FAILED'}")
        lines.append("==================================================")
        return "\n".join(lines)



def run_hardware_diagnostics(settings: Settings, timeout_s: float = 2.0) -> DiagnosticReport:
    """Check connectivity and validate configured instrument identities.

    Most instruments retain the generic nonempty ``*IDN?`` connectivity
    check.  The oscilloscope is safety-critical closed-loop feedback, so it
    must pass the same exact HDO4054 identity validation used before a run.
    """
    from gan_fet.instruments.oscilloscope import LeCroyHdo4054
    from gan_fet.instruments.scpi import ScpiTcpClient

    report = DiagnosticReport()

    for name, addr in settings.instruments.items():
        start_time = time.time()
        client: Optional[ScpiTcpClient] = None
        try:
            # Construction belongs inside the per-instrument guard: address
            # parsing can fail before a connection exists, and one malformed
            # entry must not prevent diagnostics for the remaining bench.
            client = ScpiTcpClient(name, addr.ip, addr.port, timeout=timeout_s)
            if not client.connect():
                raise ConnectionError(f"could not open connection to {addr.ip}:{addr.port}")
            response: Optional[str]
            if name == SCOPE_INSTRUMENT_KEY:
                response = LeCroyHdo4054(client).verify_identity()
            else:
                response = client.query("*IDN?")
                if not response:
                    raise ConnectionError(
                        "instrument returned an empty *IDN? response"
                    )
            elapsed_ms = (time.time() - start_time) * 1000.0

            report.results.append(
                DiagnosticResult(
                    instrument_name=name,
                    ip=addr.ip,
                    port=addr.port,
                    online=True,
                    idn_string=response,
                    response_time_ms=elapsed_ms,
                )
            )
        except Exception as exc:
            elapsed_ms = (time.time() - start_time) * 1000.0
            report.results.append(
                DiagnosticResult(
                    instrument_name=name,
                    ip=addr.ip,
                    port=addr.port,
                    online=False,
                    response_time_ms=elapsed_ms,
                    error_msg=str(exc),
                )
            )
        finally:
            if client is not None:
                client.close()

    return report
