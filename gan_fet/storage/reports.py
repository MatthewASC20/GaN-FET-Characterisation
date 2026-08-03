"""Per-device HTML report generated from the database.

Self-contained: figures are embedded as base64 PNGs, so the file can be
mailed or dropped into a shared drive as-is.
"""

from __future__ import annotations

import base64
import html
import io
import inspect
import os
import tempfile
from collections import defaultdict
from pathlib import Path

from gan_fet.core.models import RunRecord, freq_label
from gan_fet.storage.db import Database
from gan_fet.storage.paths import device_output_path

_STYLE = """
body { font-family: -apple-system, 'Segoe UI', Roboto, sans-serif;
       margin: 2em auto; max-width: 1100px; color: #222; }
h1, h2 { color: #123; }
table { border-collapse: collapse; width: 100%; font-size: 0.85em; margin: 1em 0; }
th, td { border: 1px solid #ccc; padding: 4px 8px; text-align: right; }
th { background: #eef2f7; }
td:first-child, td:nth-child(2) { text-align: left; }
img { max-width: 100%; margin: 1em 0; border: 1px solid #ddd; }
.missing { color: #999; }
.simulation-warning { background: #6a1b9a; color: white; font-weight: 700;
                      padding: 1em; text-align: center; border-radius: 4px; }
"""


def _fig_to_data_uri(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    encoded = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    fig.clear()
    return encoded


def _figure(figsize):
    """Create an isolated non-GUI canvas without changing Matplotlib's backend."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    fig = Figure(figsize=figsize)
    FigureCanvasAgg(fig)
    return fig, fig.subplots()


def _fmt(value, digits=4) -> str:
    if value is None:
        return '<span class="missing">—</span>'
    return f"{value:.{digits}g}"


def _current_vs_voltage_figures(runs: list[RunRecord]) -> list[tuple[str, str]]:
    """One figure per (frequency, duty): Iin vs test voltage, a line per
    (config, temperature)."""
    grouped: dict[tuple[int, int], dict[tuple[str, int], list[tuple[int, float]]]] = (
        defaultdict(lambda: defaultdict(list))
    )
    for run in runs:
        if run.status != "completed" or run.readings.iin is None:
            continue
        p = run.point
        grouped[(p.frequency_hz, p.duty_pct)][(p.config, p.temperature_c)].append(
            (p.voltage_v, run.readings.iin)
        )

    figures = []
    for (freq, duty), series in sorted(grouped.items()):
        fig, ax = _figure((7, 4.2))
        for (config, temp), points in sorted(series.items()):
            points.sort()
            ax.plot(
                [v for v, _ in points],
                [i * 1000 for _, i in points],
                marker="o",
                label=f"{config}, {temp}°C",
            )
        ax.set_xlabel("Test voltage — Vds peak target (V)")
        ax.set_ylabel("DC input current (mA)")
        title = f"{freq_label(freq)}, {duty}% duty"
        ax.set_title(f"DC input current vs voltage — {title}")
        ax.grid(True, alpha=0.4)
        ax.legend(fontsize=8)
        figures.append((title, _fig_to_data_uri(fig)))
    return figures


def _fsw_drift_figure(runs: list[RunRecord]) -> str | None:
    points = [
        (r.point.frequency_hz, r.readings.fsw_hz)
        for r in runs
        if r.status == "completed" and r.readings.fsw_hz
    ]
    if not points:
        return None
    fig, ax = _figure((6, 4))
    nominals = sorted({n for n, _ in points})
    data = [
        [m / 1e6 for n, m in points if n == nominal] for nominal in nominals
    ]
    labels = [freq_label(n) for n in nominals]
    # Matplotlib 3.9 renamed ``labels`` to ``tick_labels`` and 3.11 removed
    # the old spelling.  The project still supports 3.7, so select the keyword
    # exposed by the installed version instead of changing the global backend.
    label_keyword = (
        "tick_labels"
        if "tick_labels" in inspect.signature(ax.boxplot).parameters
        else "labels"
    )
    ax.boxplot(data, **{label_keyword: labels})
    for i, nominal in enumerate(nominals, start=1):
        ax.plot([i - 0.3, i + 0.3], [nominal / 1e6] * 2, "r--", lw=1)
    ax.set_ylabel("Measured switching frequency (MHz)")
    ax.set_title("Tuned frequency vs nominal (red dashed)")
    ax.grid(True, alpha=0.4)
    return _fig_to_data_uri(fig)


def generate_device_report(
    db: Database,
    device_name: str,
    dest_dir: Path,
    *,
    is_simulated: bool = False,
) -> Path:
    runs = db.runs_for_device(device_name)
    completed = [r for r in runs if r.status == "completed"]

    rows = []
    for run in completed:
        p, r = run.point, run.readings
        rows.append(
            f"<tr><td>{html.escape(p.config)}</td>"
            f"<td>{freq_label(p.frequency_hz)}</td>"
            f"<td>{p.duty_pct}</td><td>{p.temperature_c}</td><td>{p.voltage_v}</td>"
            f"<td>{_fmt(run.bus_voltage_v)}</td><td>{_fmt(run.tuned_voltage_v)}</td>"
            f"<td>{_fmt(r.vin)}</td><td>{_fmt(r.iin)}</td>"
            f"<td>{_fmt(r.fsw_hz and r.fsw_hz / 1e6, 4)}</td>"
            f"<td>{_fmt(r.irms)}</td><td>{_fmt(r.vds_pk)}</td>"
            f"<td>{_fmt(r.isw_rms)}</td></tr>"
        )

    figure_html = "".join(
        f"<h2>{html.escape(title)}</h2><img src='{uri}' alt='{html.escape(title)}'/>"
        for title, uri in _current_vs_voltage_figures(completed)
    )
    fsw_uri = _fsw_drift_figure(completed)
    if fsw_uri:
        figure_html += f"<h2>Frequency tuning</h2><img src='{fsw_uri}' alt='fsw drift'/>"

    simulation_warning = (
        '<p class="simulation-warning">SIMULATION — VIRTUAL INSTRUMENTS — '
        "NOT MEASURED DATA</p>"
        if is_simulated
        else ""
    )
    title_prefix = "SIMULATION — " if is_simulated else ""
    doc = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>{title_prefix}{html.escape(device_name)} — GaN FET characterisation report</title>
<style>{_STYLE}</style></head>
<body>
{simulation_warning}
<h1>{html.escape(device_name)}</h1>
<p>{len(completed)} completed runs ({len(runs)} total).</p>
<h2>Completed runs</h2>
<table>
<tr><th>Config</th><th>Freq</th><th>Duty %</th><th>Temp °C</th><th>V target</th>
<th>Bus V</th><th>Tuned V</th><th>Vin</th><th>Iin (A)</th>
<th>fsw (MHz)</th><th>Irms (A)</th><th>Vds pk</th><th>Isw RMS</th></tr>
{''.join(rows)}
</table>
{figure_html}
</body></html>"""

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    suffix = "SIMULATION_report.html" if is_simulated else "report.html"
    out = device_output_path(dest_dir, device_name, suffix)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        prefix=".gan-fet-report-",
        suffix=".tmp",
        dir=dest_dir,
        delete=False,
        encoding="utf-8",
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(doc)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, out)
    finally:
        temp_path.unlink(missing_ok=True)
    return out
