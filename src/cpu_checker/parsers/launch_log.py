"""ros2 launch ログから Node ↔ PID を取る。"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from cpu_checker.models import LaunchSession, NodeKind, NodeProcess
from cpu_checker.util import logger_to_path, names_match_container

RE_ROS_LOG_DIR = re.compile(r"All log files can be found below (.+)$")
# ~/.ros/log/*/launch.log は行頭に秒.小数が付く。tee した stdout には付かない。
RE_FILE_TS = re.compile(r"^\d+\.\d+\s+")
RE_ANSI = re.compile(r"\x1b\[[0-9;]*m")
RE_PROCESS_STARTED = re.compile(
    r"^\[INFO\] \[(.+)-(\d+)\]: process started with pid \[(\d+)\]"
)
RE_LOADED = re.compile(r"Loaded node '([^']+)' in container '([^']+)'")
# [name-N] [INFO] [ts] [logger]:  または  [name-N] [INFO ts] [logger] message
RE_PREFIX_LOGGER = re.compile(
    r"^\[(.+)-(\d+)\]\s+"
    r"\[(?:INFO|WARN|ERROR|DEBUG|FATAL)(?:\s+[0-9.]+)?\]"
    r"(?:\s+\[[0-9.]+\])?"
    r"\s+\[([^\]]+)\]"
)
# [INFO] [ts] [logger]:  または  [INFO ts] [logger] message
RE_ROS_LOGGER = re.compile(
    r"^\[(?:INFO|WARN|ERROR|DEBUG|FATAL)(?:\s+[0-9.]+)?\]"
    r"(?:\s+\[[0-9.]+\])?"
    r"\s+\[([^\]]+)\]"
)
RE_PROC_LOG = re.compile(r"^(.+)_(\d+)_(\d+)\.log$")
RE_SESSION_STAMP = re.compile(
    r"(\d{4}-\d{2}-\d{2})-(\d{2})-(\d{2})-(\d{2})-"
)


def _launch_log_sort_key(path: Path) -> tuple[datetime, float, str]:
    """時系列ソート用。ディレクトリ名の起動日時があればそれを優先し、なければ mtime。"""
    mtime = path.stat().st_mtime
    for name in (path.parent.name, path.name):
        stamp = RE_SESSION_STAMP.search(name)
        if stamp:
            started = datetime.strptime(
                f"{stamp.group(1)} {stamp.group(2)}:{stamp.group(3)}:{stamp.group(4)}",
                "%Y-%m-%d %H:%M:%S",
            )
            return (started, mtime, str(path))
    return (datetime.fromtimestamp(mtime), mtime, str(path))


def list_launch_logs(path: Path | None = None) -> list[Path]:
    """`--launch` を launch.log のリストに直す。ディレクトリなら中の全ファイルを時刻順。

    - ファイル → その 1 本
    - ディレクトリ → 配下の全ての `launch.log`（再帰）。起動日時 → mtime の順
    - 省略 → ~/.ros/log
    """
    target = path.expanduser() if path is not None else Path.home() / ".ros" / "log"
    try:
        if target.is_file():
            return [target]
        if target.is_dir():
            found = [item for item in target.rglob("launch.log") if item.is_file()]
            if found:
                return sorted(found, key=_launch_log_sort_key)
    except PermissionError as exc:
        raise PermissionError(f"launch ログを読めない: {target}（権限不足）") from exc
    raise FileNotFoundError(f"launch.log が見つからない: {target}")


def resolve_launch_path(path: Path | None = None) -> Path:
    """公式ログの場所を launch.log 1 本に直す（互換）。複数あるときは先頭（最古）。"""
    return list_launch_logs(path)[0]


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


def parse_launch_sessions(paths: list[Path]) -> LaunchSession:
    """複数 launch.log のセッション。起動日時は一番早いもの。"""
    if not paths:
        return LaunchSession()
    sessions = [parse_launch_session(path) for path in paths]
    started = [item.started_at for item in sessions if item.started_at]
    first = sessions[0]
    return LaunchSession(
        started_at=min(started) if started else None,
        ros_log_dir=first.ros_log_dir,
        host=next((item.host for item in sessions if item.host), None),
    )


def parse_launch_logs(paths: list[Path]) -> list[NodeProcess]:
    """複数の launch.log を時刻順にパースして NodeProcess を結合する。"""
    composable: dict[tuple[str, str], NodeProcess] = {}
    composable_order: list[tuple[str, str]] = []
    standalone: list[NodeProcess] = []
    seen_standalone: set[tuple[int, str]] = set()
    for path in paths:
        for node in parse_launch_log(path):
            if node.container_name:
                ident = (node.node_name, node.container_name)
                prev = composable.get(ident)
                if prev is None:
                    composable[ident] = node
                    composable_order.append(ident)
                elif prev.pid < 0 and node.pid > 0:
                    composable[ident] = node
                continue
            key = (node.pid, node.node_name)
            if key in seen_standalone:
                continue
            seen_standalone.add(key)
            standalone.append(node)
    return [composable[ident] for ident in composable_order] + standalone


def parse_launch_log(path: Path) -> list[NodeProcess]:
    """`process started` / `Loaded node` / コンテナロガー行を読んで結合する。"""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

    processes: dict[tuple[str, int], int] = {}
    loaded: list[tuple[str, str]] = []
    loggers: dict[tuple[str, int], list[str]] = defaultdict(list)

    for raw in lines:
        line = RE_ANSI.sub("", RE_FILE_TS.sub("", raw, count=1))
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
    _merge_process_logs(loggers, processes, session.ros_log_dir, launch_log_path=path)

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
    launch_log_path: Path | None = None,
) -> None:
    """セッション隣の {exec}_{pid}_{ts}.log からロガー名を足す。

    launch.log には子プロセスの stdout が一部しか残らない。
    rclcpp は ~/.ros/log/ 直下（セッションディレクトリの親）に書く。
    ファイル名の実行名は python3 など実体なので、PID だけで探す。
    コピーしたログでは、launch.log の親（--launch で渡したディレクトリ）も探す。
    """
    candidates: list[Path] = []
    if ros_log_dir:
        session_dir = Path(ros_log_dir)
        candidates.extend([session_dir.parent, session_dir])
    if launch_log_path is not None:
        here = launch_log_path.parent
        candidates.extend([here, here.parent])
    search_dirs = []
    denied: list[Path] = []
    for candidate in candidates:
        try:
            readable = candidate.is_dir()
        except PermissionError:
            denied.append(candidate)
            continue
        if readable and candidate not in search_dirs:
            search_dirs.append(candidate)
    if not search_dirs:
        for item in denied:
            print(f"warning: プロセスログを読めない: {item}", file=sys.stderr)
    if not search_dirs:
        if ros_log_dir or launch_log_path:
            print(
                f"warning: プロセスログディレクトリに入れない: {ros_log_dir or launch_log_path}",
                file=sys.stderr,
            )
        return

    pid_to_key = {pid: key for key, pid in processes.items()}
    for directory in search_dirs:
        try:
            paths = list(directory.glob("*.log"))
        except PermissionError:
            print(f"warning: プロセスログを読めない: {directory}", file=sys.stderr)
            continue
        for path in paths:
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
    for raw in text.splitlines():
        line = RE_ANSI.sub("", raw)
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
