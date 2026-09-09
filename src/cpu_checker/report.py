"""report サブコマンドの組み立て。"""

from __future__ import annotations

import csv
from pathlib import Path

from cpu_checker.join import join_by_pid, unmapped_nodes
from cpu_checker.models import LaunchSession, NodeProcess, PidGroup
from cpu_checker.parsers.launch_log import parse_launch_log, parse_launch_session
from cpu_checker.parsers.top_log import parse_top_log
from cpu_checker.util import affinity_label, format_core_map, per_core_cpu, saturated_cores, used_core_label
from cpu_checker.visualize import write_report


def build_report(
    launch_path: Path | None,
    top_path: Path | None,
    out_html: Path,
    include_unmapped: bool = True,
    top_n: int = 15,
) -> list[PidGroup]:
    out_html.parent.mkdir(parents=True, exist_ok=True)
    session = parse_launch_session(launch_path) if launch_path else LaunchSession()
    nodes = parse_launch_log(launch_path) if launch_path else []
    samples = []
    notes = [f"launch={launch_path.name}" if launch_path else "launch=(none)"]
    if top_path is not None:
        samples = parse_top_log(top_path, date_hint=session.started_at)
        notes.append(f"top={top_path.name} ({len(samples)} samples)")
    groups = join_by_pid(nodes, samples, include_unmapped=include_unmapped)
    missing = unmapped_nodes(nodes)
    write_report(
        groups,
        out_html,
        session=session,
        unmapped=missing,
        top_n=top_n,
        source_note=" / ".join(notes),
    )
    _write_csvs(out_html.parent, groups, missing)
    return groups


def _write_csvs(
    out_dir: Path,
    groups: list[PidGroup],
    missing: list[NodeProcess],
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    pid_csv = out_dir / "pid_cpu.csv"
    with pid_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "pid",
                "kind",
                "display_names",
                "container",
                "mean_cpu",
                "max_cpu",
                "affinity",
                "used_cores",
                "top_cores",
                "saturated_cores",
                "sample_count",
            ]
        )
        for group in groups:
            buckets = per_core_cpu(group.samples)
            writer.writerow(
                [
                    group.pid,
                    group.kind_label,
                    " | ".join(group.display_names),
                    group.container_name or "",
                    f"{group.mean_cpu:.2f}" if group.samples else "",
                    f"{group.max_cpu:.2f}" if group.samples else "",
                    affinity_label(group.samples) if group.samples else "",
                    used_core_label(buckets) if group.samples else "",
                    format_core_map(buckets, max) if group.samples else "",
                    ",".join(saturated_cores(buckets)),
                    len(group.samples),
                ]
            )

    members_csv = out_dir / "container_members.csv"
    with members_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["pid", "container", "node_name", "launch_name", "launch_index"])
        for group in groups:
            if not group.is_composable:
                continue
            for member in group.members:
                writer.writerow(
                    [
                        group.pid,
                        group.container_name or "",
                        member.node_name,
                        member.launch_name,
                        member.launch_index,
                    ]
                )
        for node in missing:
            writer.writerow(
                [
                    "",
                    node.container_name or "",
                    node.node_name,
                    node.launch_name,
                    node.launch_index,
                ]
            )
