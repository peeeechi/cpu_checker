"""PidGroup からレポート用データを組み立て、Jinja2 で HTML を書く。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from cpu_checker.models import LaunchSession, NodeProcess, PidGroup
from cpu_checker.util import (
    affinity_label,
    format_core_map,
    format_display_names,
    per_core_cpu,
    saturated_cores,
    short_node_name,
    used_core_label,
)

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
TOP_N = 15
SAT_THRESHOLD = 100.0


def _group_label(group: PidGroup, max_show: int = 1) -> str:
    if group.container_name:
        return f"{short_node_name(group.container_name)} ({len(group.display_names)} nodes)"
    return format_display_names(group.display_names, max_show, short=True)


def write_report(
    groups: list[PidGroup],
    out: Path,
    session: LaunchSession | None = None,
    unmapped: list[NodeProcess] | None = None,
    top_n: int = TOP_N,
    source_note: str = "",
) -> None:
    context = _report_context(groups, session, unmapped or [], top_n, source_note)
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html", "xml"]),
    )
    html = env.get_template("report.html").render(context)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")


def _report_context(
    groups: list[PidGroup],
    session: LaunchSession | None,
    unmapped: list[NodeProcess],
    top_n: int,
    source_note: str,
) -> dict:
    ranked = _rank_groups(groups)
    heavy = [group for group in ranked if group.samples][:top_n]
    return {
        **_summary_data(ranked, session, source_note),
        "pid_rows": _pid_rows(ranked),
        "member_rows": _member_rows(ranked, unmapped),
        "charts": _chart_data(heavy, ranked),
    }


def _rank_groups(groups: list[PidGroup]) -> list[PidGroup]:
    return sorted(groups, key=lambda g: (g.mean_cpu, g.max_cpu), reverse=True)


def _summary_data(
    groups: list[PidGroup],
    session: LaunchSession | None,
    source_note: str,
) -> dict:
    node_count = sum(len(group.members) for group in groups)
    container_count = len({group.container_name for group in groups if group.container_name})
    sample_count = sum(len(group.samples) for group in groups)
    has_core = any(sample.last_cpu is not None for group in groups for sample in group.samples)
    started = (
        session.started_at.strftime("%Y-%m-%d %H:%M:%S")
        if session and session.started_at
        else "-"
    )
    top_processes = [
        {
            "label": _group_label(group),
            "pid": group.pid,
            "mean_cpu": f"{group.mean_cpu:.0f}",
            "max_cpu": f"{group.max_cpu:.0f}",
        }
        for group in [item for item in groups if item.samples][:3]
    ]
    sat_items = []
    for group in groups:
        sat = saturated_cores(per_core_cpu(group.samples), SAT_THRESHOLD)
        if sat:
            sat_items.append(
                {"cores": ", ".join(sat), "label": _group_label(group), "pid": group.pid}
            )
    if sat_items:
        warning = {"kind": "sat", "entries": sat_items}
    elif sample_count and not has_core:
        warning = {"kind": "no_core", "entries": []}
    elif sample_count == 0:
        warning = {"kind": "no_sample", "entries": []}
    else:
        warning = {"kind": None, "entries": []}
    return {
        "started": started,
        "process_count": len(groups),
        "node_count": node_count,
        "container_count": container_count,
        "sample_count": sample_count,
        "source_note": source_note or "-",
        "top_processes": top_processes,
        "warning": warning,
    }


def _pid_rows(groups: list[PidGroup]) -> list[dict]:
    rows: list[dict] = []
    for group in groups:
        buckets = per_core_cpu(group.samples)
        extra = max(0, len(group.display_names) - 4)
        rows.append(
            {
                "names": group.display_names[:4],
                "extra_nodes": extra,
                "pid": group.pid,
                "kind": group.kind_label,
                "mean": f"{group.mean_cpu:.1f}" if group.samples else "-",
                "max": f"{group.max_cpu:.1f}" if group.samples else "-",
                "affinity": affinity_label(group.samples) if group.samples else "-",
                "used_cores": used_core_label(buckets) if group.samples else "-",
                "top_cores": format_core_map(buckets, max) if group.samples else "-",
                "saturated": ", ".join(saturated_cores(buckets, SAT_THRESHOLD)) or "-",
                "sample_count": len(group.samples),
            }
        )
    return rows


def _member_rows(groups: list[PidGroup], unmapped: list[NodeProcess]) -> list[dict]:
    rows: list[dict] = []
    for group in groups:
        if not group.is_composable:
            continue
        mean_txt = f"{group.mean_cpu:.1f}" if group.samples else "-"
        max_txt = f"{group.max_cpu:.1f}" if group.samples else "-"
        for index, member in enumerate(group.members or []):
            rows.append(
                {
                    "node_name": member.node_name,
                    "pid": str(group.pid) if index == 0 else "",
                    "container": group.container_name or "-",
                    "mean": mean_txt if index == 0 else "",
                    "max": max_txt if index == 0 else "",
                    "unmapped": False,
                }
            )
    for node in unmapped:
        rows.append(
            {
                "node_name": node.node_name,
                "pid": "-",
                "container": node.container_name or "-",
                "mean": "",
                "max": "",
                "unmapped": True,
            }
        )
    return rows


def _chart_data(heavy: list[PidGroup], ranked: list[PidGroup]) -> dict:
    return {
        "ranking": _ranking_data(heavy),
        "timeseries": _timeseries_data(heavy),
        "heatmap": _heatmap_data(ranked),
    }


def _ranking_data(groups: list[PidGroup]) -> dict:
    return {
        "labels": [f"{_group_label(group)} [{group.pid}]" for group in groups],
        "means": [group.mean_cpu for group in groups],
        "maxes": [group.max_cpu for group in groups],
    }


def _timeseries_data(groups: list[PidGroup]) -> dict:
    series: list[dict] = []
    for group in groups:
        if not group.samples:
            continue
        first = group.samples[0].timestamp
        series.append(
            {
                "name": _group_label(group),
                "pid": group.pid,
                "x": [(sample.timestamp - first).total_seconds() for sample in group.samples],
                "y": [sample.cpu_percent for sample in group.samples],
                "cores": [
                    "" if sample.last_cpu is None else f"CPU{sample.last_cpu}"
                    for sample in group.samples
                ],
            }
        )
    return {"series": series}


def _heatmap_data(groups: list[PidGroup]) -> dict | None:
    cells: dict[tuple[int, int], list[float]] = {}
    hover: dict[tuple[int, int], list[str]] = {}
    t0: datetime | None = None
    for group in groups:
        for sample in group.samples:
            if sample.last_cpu is None:
                continue
            if t0 is None:
                t0 = sample.timestamp
            elapsed = int((sample.timestamp - t0).total_seconds())
            key = (sample.last_cpu, elapsed)
            cells.setdefault(key, []).append(min(sample.cpu_percent, 100.0))
            hover.setdefault(key, []).append(
                f"{_group_label(group)} ({sample.cpu_percent:.0f}%)"
            )
    if not cells or t0 is None:
        return None
    cores = sorted({core for core, _ in cells})
    times = sorted({elapsed for _, elapsed in cells})
    z = []
    text = []
    for core in cores:
        row = []
        trow = []
        for elapsed in times:
            vals = cells.get((core, elapsed), [])
            row.append(min(sum(vals), 100.0) if vals else None)
            names = hover.get((core, elapsed), [])
            trow.append("<br>".join(names[:3]) + (f"<br>他{len(names)-3}" if len(names) > 3 else ""))
        z.append(row)
        text.append(trow)
    return {
        "x": times,
        "y": [f"CPU{c}" for c in cores],
        "z": z,
        "text": text,
    }
