"""Per-device HTML report generated from the database.

Self-contained: figures are embedded as base64 PNGs, so the file can be
mailed or dropped into a shared drive as-is.
"""

from __future__ import annotations

import base64
import html
import io
from collections import defaultdict
from pathlib import Path

from gan_fet.core.models import RunRecord, freq_label
from gan_fet.storage.db import Database

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
"""


def _fig_to_data_uri(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    import matplotlib.pyplot as plt

    plt.close(fig)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _fmt(value, digits=4) -> str:
    if value is None:
        return '<span class="missing">—</span>'
    return f"{value:.{digits}g}"


def _current_vs_voltage_figures(runs: list[RunRecord]) -> list[tuple[str, str]]:
    """One figure per (frequency, duty): Iin vs test voltage, a line per
    (config, temperature)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

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
        fig, ax = plt.subplots(figsize=(7, 4.2))
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
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    points = [
        (r.point.frequency_hz, r.readings.fsw_hz)
        for r in runs
        if r.status == "completed" and r.readings.fsw_hz
    ]
    if not points:
        return None
    fig, ax = plt.subplots(figsize=(6, 4))
    nominals = sorted({n for n, _ in points})
    data = [
        [m / 1e6 for n, m in points if n == nominal] for nominal in nominals
    ]
    ax.boxplot(data, tick_labels=[freq_label(n) for n in nominals])
    for i, nominal in enumerate(nominals, start=1):
        ax.plot([i - 0.3, i + 0.3], [nominal / 1e6] * 2, "r--", lw=1)
    ax.set_ylabel("Measured switching frequency (MHz)")
    ax.set_title("Tuned frequency vs nominal (red dashed)")
    ax.grid(True, alpha=0.4)
    return _fig_to_data_uri(fig)


def generate_device_report(db: Database, device_name: str, dest_dir: Path) -> Path:
    runs = db.runs_for_device(device_name)
    completed = [r for r in runs if r.status == "completed"]

    rows = []
    for run in completed:
        p, r = run.point, run.readings
        rows.append(
            f"<tr><td>{html.escape(p.config)}</td>"
            f"<td>{freq_label(p.frequency_hz)}</td>"
            f"<td>{p.duty_pct}</td><td>{p.temperature_c}</td><td>{p.voltage_v}</td>"
            f"<td>{_fmt(run.bus_voltage_v)}</td><td>{_fmt(run.v_zvs)}</td>"
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

    doc = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>{html.escape(device_name)} — GaN FET characterisation report</title>
<style>{_STYLE}</style></head>
<body>
<h1>{html.escape(device_name)}</h1>
<p>{len(completed)} completed runs ({len(runs)} total).</p>
<h2>Completed runs</h2>
<table>
<tr><th>Config</th><th>Freq</th><th>Duty %</th><th>Temp °C</th><th>V target</th>
<th>Bus V</th><th>V<sub>ZVS</sub></th><th>Vin</th><th>Iin (A)</th>
<th>fsw (MHz)</th><th>Irms (A)</th><th>Vds pk</th><th>Isw RMS</th></tr>
{''.join(rows)}
</table>
{figure_html}
</body></html>"""

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / f"{device_name}_report.html"
    out.write_text(doc, encoding="utf-8")
    return out
