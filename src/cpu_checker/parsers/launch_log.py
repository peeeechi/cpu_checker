"""ros2 launch ログから Node ↔ PID を取る。"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from cpu_checker.models import LaunchSession, NodeKind, NodeProcess
from cpu_checker.util import logger_to_path, names_match_container

RE_ROS_LOG_DIR = re.compile(r"All log files can be found below (.+)$")
# ~/.ros/log/*/launch.log は行頭に秒.小数が付く。tee した stdout には付かない。
RE_FILE_TS = re.compile(r"^\d+\.\d+\s+")
RE_PROCESS_STARTED = re.compile(
    r"^\[INFO\] \[(.+)-(\d+)\]: process started with pid \[(\d+)\]"
)
RE_LOADED = re.compile(r"Loaded node '([^']+)' in container '([^']+)'")
RE_PREFIX_LOGGER = re.compile(
    r"^\[(.+)-(\d+)\]\s+\[(?:INFO|WARN|ERROR|DEBUG|FATAL)\]"
    r"(?:\s+\[[0-9.]+\])?\s+\[([^\]]+)\]:"
)
# ~/.ros/log/{exec}_{pid}_{ts}.log の本体。launch 接頭辞は無い。
RE_ROS_LOGGER = re.compile(
    r"^\[(?:INFO|WARN|ERROR|DEBUG|FATAL)\]\s+\[[0-9.]+\]\s+\[([^\]]+)\]:"
)
RE_PROC_LOG = re.compile(r"^(.+)_(\d+)_(\d+)\.log$")
RE_SESSION_STAMP = re.compile(
    r"(\d{4}-\d{2}-\d{2})-(\d{2})-(\d{2})-(\d{2})-"
)


def resolve_launch_path(path: Path | None = None) -> Path:
    """公式ログの場所を launch.log ファイルに直す。

    - ファイル → そのまま
    - セッションディレクトリ（中に launch.log）→ その launch.log
    - ~/.ros/log など（*/launch.log が並ぶ）→ 更新が一番新しいもの
    - 省略 → ~/.ros/log
    """
    target = path.expanduser() if path is not None else Path.home() / ".ros" / "log"
    if target.is_file():
        return target
    if target.is_dir():
        direct = target / "launch.log"
        if direct.is_file():
            return direct
        newest = max(target.glob("*/launch.log"), key=lambda item: item.stat().st_mtime, default=None)
        if newest is not None:
            return newest
    raise FileNotFoundError(f"launch.log が見つからない: {target}")


def parse_launch_session(path: Path) -> LaunchSession:
    text = path.read_text(encoding="utf-8", errors="replace")
    ros_log_dir = None
    for line in text.splitlines():
        matched = RE_ROS_LOG_DIR.search(line)
        if matched:
            ros_log_dir = matched.group(1).strip()
            break
    started_at = None
    host = None
    if ros_log_dir:
        base = Path(ros_log_dir).name
        stamp = RE_SESSION_STAMP.search(base)
        if stamp:
            started_at = datetime.strptime(
                f"{stamp.group(1)} {stamp.group(2)}:{stamp.group(3)}:{stamp.group(4)}",
                "%Y-%m-%d %H:%M:%S",
            )
        parts = base.split("-")
        if parts:
            host = parts[-2] if len(parts) >= 2 else None
    return LaunchSession(started_at=started_at, ros_log_dir=ros_log_dir, host=host)


def parse_launch_log(path: Path) -> list[NodeProcess]:
    """`process started` / `Loaded node` / コンテナロガー行を読んで結合する。"""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

    processes: dict[tuple[str, int], int] = {}
    loaded: list[tuple[str, str]] = []
    loggers: dict[tuple[str, int], list[str]] = defaultdict(list)

    for raw in lines:
        line = RE_FILE_TS.sub("", raw, count=1)
        started = RE_PROCESS_STARTED.search(line)
        if started:
            key = (started.group(1), int(started.group(2)))
            processes[key] = int(started.group(3))
            continue
        loaded_m = RE_LOADED.search(line)
        if loaded_m:
            loaded.append((loaded_m.group(1), loaded_m.group(2)))
            continue
        logger_m = RE_PREFIX_LOGGER.search(line)
        if logger_m:
            key = (logger_m.group(1), int(logger_m.group(2)))
            logger_name = logger_m.group(3)
            if logger_name not in loggers[key]:
                loggers[key].append(logger_name)

    session = parse_launch_session(path)
    _merge_process_logs(loggers, processes, session.ros_log_dir)

    containers = {container for _, container in loaded}
    node_to_container = {node_name: container for node_name, container in loaded}
    container_to_proc: dict[str, tuple[str, int, int]] = {}
    used_keys: set[tuple[str, int]] = set()

    for key, names in loggers.items():
        if key not in processes:
            continue
        for logger_name in names:
            for container in containers:
                if names_match_container(logger_name, container):
                    if container not in container_to_proc:
                        container_to_proc[container] = (key[0], key[1], processes[key])
                        used_keys.add(key)
                    break

    # コンテナ自身のロガーが無いとき、所属 Node のロガーで補う
    for key, names in loggers.items():
        if key not in processes or key in used_keys:
            continue
        for logger_name in names:
            node_path = logger_to_path(logger_name)
            container = node_to_container.get(node_path)
            if container and container not in container_to_proc:
                container_to_proc[container] = (key[0], key[1], processes[key])
                used_keys.add(key)
                break

    nodes: list[NodeProcess] = []
    seen_composable: set[tuple[str, int]] = set()
    for node_name, container in loaded:
        mapped = container_to_proc.get(container)
        if mapped:
            launch_name, launch_index, pid = mapped
            logger_name = path_to_logger_safe(container)
        else:
            launch_name, launch_index, pid = container, -1, -1
            logger_name = path_to_logger_safe(container)
        item = (node_name, pid)
        if item in seen_composable:
            continue
        seen_composable.add(item)
        nodes.append(
            NodeProcess(
                node_name=node_name,
                kind=NodeKind.COMPOSABLE,
                pid=pid,
                launch_name=launch_name,
                launch_index=launch_index,
                container_name=container,
                ros_logger=logger_name,
            )
        )

    for key, pid in processes.items():
        launch_name, launch_index = key
        if key in used_keys:
            continue
        if _is_container_exec(launch_name):
            nodes.append(
                NodeProcess(
                    node_name=f"/{launch_name}",
                    kind=NodeKind.STANDALONE,
                    pid=pid,
                    launch_name=launch_name,
                    launch_index=launch_index,
                    container_name=None,
                    ros_logger=None,
                )
            )
            continue
        ros_name = _standalone_name(launch_name, loggers.get(key, []))
        nodes.append(
            NodeProcess(
                node_name=ros_name,
                kind=NodeKind.STANDALONE,
                pid=pid,
                launch_name=launch_name,
                launch_index=launch_index,
                container_name=None,
                ros_logger=loggers[key][0] if loggers.get(key) else None,
            )
        )

    return nodes


def _merge_process_logs(
    loggers: dict[tuple[str, int], list[str]],
    processes: dict[tuple[str, int], int],
    ros_log_dir: str | None,
) -> None:
    """セッション隣の {exec}_{pid}_{ts}.log からロガー名を足す。

    launch.log には子プロセスの stdout が一部しか残らない。
    rclcpp は ~/.ros/log/ 直下（セッションディレクトリの親）に書く。
    ファイル名の実行名は python3 など実体なので、PID だけで探す。
    """
    if not ros_log_dir:
        return
    session_dir = Path(ros_log_dir)
    search_dirs = []
    for candidate in (session_dir.parent, session_dir):
        if candidate.is_dir() and candidate not in search_dirs:
            search_dirs.append(candidate)
    if not search_dirs:
        return

    pid_to_key = {pid: key for key, pid in processes.items()}
    for directory in search_dirs:
        for path in directory.glob("*.log"):
            matched = RE_PROC_LOG.match(path.name)
            if not matched:
                continue
            pid = int(matched.group(2))
            key = pid_to_key.get(pid)
            if key is None:
                continue
            for logger_name in _loggers_from_process_file(path):
                if logger_name not in loggers[key]:
                    loggers[key].append(logger_name)


def _loggers_from_process_file(path: Path) -> list[str]:
    names: list[str] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        matched = RE_ROS_LOGGER.search(line)
        if not matched:
            continue
        name = matched.group(1)
        if name not in names:
            names.append(name)
    return names


def path_to_logger_safe(path: str) -> str:
    return path.lstrip("/").replace("/", ".")


def _is_container_exec(launch_name: str) -> bool:
    return launch_name.startswith("component_container")


def _standalone_name(launch_name: str, loggers: list[str]) -> str:
    for logger_name in loggers:
        if logger_name.startswith("rcl") or logger_name.startswith("launch"):
            continue
        return logger_to_path(logger_name)
    return f"/{launch_name}"
