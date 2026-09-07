"""NodeProcess と ProcessSample を PID で結合する。"""

from __future__ import annotations

from collections import defaultdict

from cpu_checker.models import NodeProcess, PidGroup, ProcessSample


def join_by_pid(
    nodes: list[NodeProcess],
    samples: list[ProcessSample],
    include_unmapped: bool = True,
) -> list[PidGroup]:
    """同じ PID を 1 グループにする。Composable の所属 Node を members に入れる。

    launch に無い PID も、既定ではサンプルがあればグループにする。
    """
    groups: dict[int, PidGroup] = {}
    for node in nodes:
        if node.pid < 0:
            continue
        group = groups.get(node.pid)
        if group is None:
            group = PidGroup(pid=node.pid, container_name=node.container_name)
            groups[node.pid] = group
        group.members.append(node)
        if node.container_name:
            group.container_name = node.container_name

    samples_by_pid: dict[int, list[ProcessSample]] = defaultdict(list)
    for sample in samples:
        samples_by_pid[sample.pid].append(sample)

    for pid, group in groups.items():
        group.samples = sorted(samples_by_pid.get(pid, []), key=lambda item: item.timestamp)

    if include_unmapped:
        for pid, pid_samples in samples_by_pid.items():
            if pid not in groups:
                groups[pid] = PidGroup(
                    pid=pid,
                    samples=sorted(pid_samples, key=lambda item: item.timestamp),
                )

    return sorted(groups.values(), key=lambda g: g.pid)


def unmapped_nodes(nodes: list[NodeProcess]) -> list[NodeProcess]:
    return [node for node in nodes if node.pid < 0]
