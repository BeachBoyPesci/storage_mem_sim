"""SimulationResult — output of one discrete-event simulation run."""

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List

from ..memory_pool import MemoryRequestMetrics


@dataclass
class SimulationResult:
    """Aggregated output from a SimpleSimulator run."""

    request_metrics: List[MemoryRequestMetrics] = field(default_factory=list)
    per_source: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    makespan: float = 0.0
    scheduled_finish_events: int = 0
    stale_finish_events: int = 0

    def save_json(self, filepath: str) -> None:
        """Write results as JSON to *filepath*."""
        os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
        with open(filepath, "w") as f:
            json.dump({
                "makespan_s": self.makespan,
                "scheduled_finish_events": self.scheduled_finish_events,
                "stale_finish_events": self.stale_finish_events,
                "per_source": {
                    s: {
                        "avg_latency_s": v["avg_latency"],
                        "avg_contention_delay_s": v["avg_contention_delay"],
                        "total_bytes": v["total_bytes"],
                        "count": v["count"],
                    }
                    for s, v in self.per_source.items()
                },
                "request_metrics": [
                    {
                        "request_id": m.request_id,
                        "source_id": m.source_id,
                        "mem_engine_id": m.mem_engine_id,
                        "arrival_time_s": m.arrival_time,
                        "finish_time_s": m.finish_time,
                        "size_bytes": m.size,
                        "latency_s": m.latency,
                        "standalone_time_s": m.standalone_time,
                        "contention_delay_s": m.contention_delay,
                        "average_bandwidth_Bps": m.average_bandwidth,
                    }
                    for m in self.request_metrics
                ],
            }, f, indent=2)

    def save_html(self, filepath: str) -> None:
        """Write a self-contained HTML report to *filepath*."""
        os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)

        metrics = sorted(self.request_metrics,
                         key=lambda m: (m.mem_engine_id, m.arrival_time))
        # Data for charts.
        labels = [f"eng {m.mem_engine_id} | {m.source_id}" for m in metrics]
        starts_ns = [m.arrival_time * 1e9 for m in metrics]
        durs_ns = [m.latency * 1e9 for m in metrics]
        standalone_ns = [m.standalone_time * 1e9 for m in metrics]
        sources = [m.source_id for m in metrics]
        engines = [m.mem_engine_id for m in metrics]

        unique_sources = sorted(set(sources))
        colors = [
            f"hsl({360 * i // max(len(unique_sources), 1)}, 60%, 55%)"
            for i in range(len(unique_sources))
        ]
        color_map = dict(zip(unique_sources, colors))
        src_colors = [color_map[s] for s in sources]

        html = _HTML_TEMPLATE.format(
            makespan=f"{self.makespan * 1e9:.1f}",
            n_requests=len(metrics),
            scheduled=self.scheduled_finish_events,
            stale=self.stale_finish_events,
            chart_height=max(80, 40 * len(metrics)),
            labels_json=json.dumps(labels),
            starts_json=json.dumps(starts_ns),
            durs_json=json.dumps(durs_ns),
            standalone_json=json.dumps(standalone_ns),
            colors_json=json.dumps(src_colors),
            sources_json=json.dumps(sources),
            engines_json=json.dumps(engines),
            per_source_rows=_build_source_rows(self.per_source),
        )
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(html)


def _build_source_rows(per_source: dict) -> str:
    rows = []
    for s in sorted(per_source):
        v = per_source[s]
        rows.append(
            f"<tr><td>{s}</td><td>{v['count']}</td>"
            f"<td>{v['avg_latency'] * 1e9:.1f}</td>"
            f"<td>{v['avg_contention_delay'] * 1e9:.1f}</td>"
            f"<td>{v['total_bytes']:,}</td></tr>"
        )
    return "\n".join(rows)


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MemEngine DES Result</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js">
</script>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         max-width: 1200px; margin: 0 auto; padding: 20px; background: #f8f8f8; }}
  h1 {{ color: #222; border-bottom: 2px solid #ddd; padding-bottom: 8px; }}
  .summary {{ display: flex; gap: 16px; flex-wrap: wrap; margin: 16px 0; }}
  .card {{ background: #fff; border-radius: 8px; padding: 16px 24px;
            box-shadow: 0 1px 3px rgba(0,0,0,.1); min-width: 140px; }}
  .card .value {{ font-size: 24px; font-weight: bold; color: #333; }}
  .card .label {{ font-size: 12px; color: #888; text-transform: uppercase; }}
  .chart-box {{ background: #fff; border-radius: 8px; padding: 16px;
                 box-shadow: 0 1px 3px rgba(0,0,0,.1); margin: 16px 0; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid #eee; }}
  th {{ background: #f0f0f0; font-weight: 600; }}
</style>
</head>
<body>
<h1>MemEngine DES Simulation Result</h1>
<div class="summary">
  <div class="card"><div class="value">{makespan} ns</div><div class="label">Makespan</div></div>
  <div class="card"><div class="value">{n_requests}</div><div class="label">Requests</div></div>
  <div class="card"><div class="value">{scheduled}</div><div class="label">Scheduled Finishes</div></div>
  <div class="card"><div class="value">{stale}</div><div class="label">Stale Events</div></div>
</div>
<div class="chart-box">
  <h2>Request Gantt Chart</h2>
  <canvas id="gantt" height="{chart_height}"></canvas>
</div>
<div class="chart-box">
  <h2>Latency Breakdown (standalone + contention)</h2>
  <canvas id="latency"></canvas>
</div>
<div class="chart-box">
  <h2>Per-Source Summary</h2>
  <table><thead><tr>
    <th>Source</th><th>Count</th><th>Avg Latency (ns)</th>
    <th>Avg Contention (ns)</th><th>Total Bytes</th>
  </tr></thead><tbody>
{per_source_rows}
  </tbody></table>
</div>
<script>
const labels = {labels_json};
const starts = {starts_json};
const durs = {durs_json};
const standalone = {standalone_json};
const colors = {colors_json};
const sources = {sources_json};
const engines = {engines_json};
new Chart(document.getElementById('gantt'), {{
  type: 'bar',
  data: {{
    labels: labels,
    datasets: [
      {{
        label: 'Standalone',
        data: starts.map((s, i) => [s, s + standalone[i]]),
        backgroundColor: colors.map(c => c.replace('55%', '65%')),
        borderSkipped: false, borderRadius: 0,
      }},
      {{
        label: 'Contention',
        data: starts.map((s, i) => [s + standalone[i], s + durs[i]]),
        backgroundColor: colors.map(c => c.replace('55%', '60%')),
        borderSkipped: false, borderRadius: 0,
      }},
    ],
  }},
  options: {{
    indexAxis: 'y', responsive: true,
    scales: {{
      x: {{ title: {{ display: true, text: 'Time (ns)' }} }},
      y: {{ ticks: {{ callback: v => labels[v] }} }},
    }},
    plugins: {{
      tooltip: {{ callbacks: {{
        label: ctx => {{
          const i = ctx.dataIndex;
          const arr = starts[i].toFixed(1);
          const fin = (starts[i] + durs[i]).toFixed(1);
          return [
            sources[i] + ' (eng ' + engines[i] + ')',
            'arrival:  ' + arr + ' ns',
            'finish:   ' + fin + ' ns',
            'latency:  ' + durs[i].toFixed(1) + ' ns',
            'standalone: ' + standalone[i].toFixed(1) + ' ns',
            'contention: ' + (durs[i] - standalone[i]).toFixed(1) + ' ns',
          ];
        }},
      }} }},
    }},
  }},
}});
new Chart(document.getElementById('latency'), {{
  type: 'bar',
  data: {{
    labels: labels,
    datasets: [
      {{ label: 'Standalone (ns)', data: standalone, backgroundColor: '#66c2a5' }},
      {{ label: 'Contention (ns)', data: labels.map((_, i) => durs[i] - standalone[i]), backgroundColor: '#fc8d62' }},
    ],
  }},
  options: {{
    responsive: true,
    scales: {{
      x: {{ stacked: true, ticks: {{ maxRotation: 60, callback: v => labels[v].slice(-18) }} }},
      y: {{ stacked: true, title: {{ display: true, text: 'Time (ns)' }} }},
    }},
  }},
}});
</script>
</body></html>"""
