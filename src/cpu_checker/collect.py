"""実行中の /proc をサンプリングする。"""

from __future__ import annotations

import csv
import os
import time
from datetime import datetime
from pathlib import Path

from cpu_checker.models import ProcessSample

CLK_TCK = os.sysconf("SC_CLK_TCK")


def sample_proc(
    interval_sec: float,
    duration_sec: float | None = None,
) -> list[ProcessSample]:
    """マシン上の全プロセスの Cpus_allowed_list / last CPU / %CPU を interval ごとに取る。

    毎周 `/proc` を走査するので、開始後に立ち上がったプロセスも次の周から対象になる。
    PID が消えたら履歴を捨て、同じ番号の再利用で古いティックを混ぜない。
    """
    prev: dict[int, tuple[float, float]] = {}
    samples: list[ProcessSample] = []
    started = time.monotonic()
    try:
        while True:
            now = datetime.now()
            mono = time.monotonic()
            current: dict[int, tuple[float, float]] = {}
            for pid in _list_pids():
                snap = _read_pid(pid)
                if snap is None:
                    continue
                ticks, last_cpu, allowed, command, rss = snap
                prev_item = prev.get(pid)
                if prev_item is not None:
                    dt = mono - prev_item[1]
                    if dt > 0:
                        pcpu = (ticks - prev_item[0]) / CLK_TCK / dt * 100.0
                        samples.append(
                            ProcessSample(
                                timestamp=now,
                                pid=pid,
                                cpu_percent=pcpu,
                                command=command,
                                res_kib=rss,
                                last_cpu=last_cpu,
                                cpu_allowed=allowed,
                            )
                        )
                current[pid] = (ticks, mono)
            prev = current
            if duration_sec is not None and (time.monotonic() - started) >= duration_sec:
                break
            time.sleep(interval_sec)
    except KeyboardInterrupt:
        pass
    return samples


def write_samples(samples: list[ProcessSample], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["timestamp", "PID", "%CPU", "COMMAND", "Psr", "Cpus_allowed", "RES"]
        )
        for sample in samples:
            writer.writerow(
                [
                    sample.timestamp.strftime("%Y-%m-%dT%H:%M:%S"),
                    sample.pid,
                    f"{sample.cpu_percent:.2f}",
                    sample.command,
                    "" if sample.last_cpu is None else sample.last_cpu,
                    sample.cpu_allowed or "",
                    "" if sample.res_kib is None else f"{sample.res_kib:.1f}",
                ]
            )


def _list_pids() -> list[int]:
    """`/proc` 直下の数値ディレクトリ = プロセス（スレッドは含まない）。"""
    try:
        return [int(name) for name in os.listdir("/proc") if name.isdigit()]
    except OSError:
        return []


def _read_pid(pid: int) -> tuple[float, int, str, str, float] | None:
    """`/proc` から 1 PID の瞬間値を読む。

    Args:
        pid: 対象プロセスの PID。

    Returns:
        読めたとき:
            ticks: utime + stime（クロックティック。%CPU の差分計算用）
            last_cpu: 最後に走ったコア番号（stat の processor）
            allowed: 許可コア（status の Cpus_allowed_list。例: ``0-19``）
            command: 実行名（stat の comm）
            rss: 常駐メモリ VmRSS（KiB）。無ければ 0.0
        プロセスが無い、または読めないときは None。
    """
    stat_path = Path(f"/proc/{pid}/stat")
    status_path = Path(f"/proc/{pid}/status")
    if not stat_path.exists():
        return None
    try:
        stat_text = stat_path.read_text(encoding="utf-8", errors="replace")
        comm_end = stat_text.rfind(")")
        rest = stat_text[comm_end + 2 :].split()
        # after comm: state(0) ... utime(11) stime(12) ... processor(36)
        utime = float(rest[11])
        stime = float(rest[12])
        last_cpu = int(rest[36])
        command = stat_text[stat_text.find("(") + 1 : comm_end]
        rss = None
        allowed = ""
        if status_path.exists():
            for line in status_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("Cpus_allowed_list:"):
                    allowed = line.split(":", 1)[1].strip()
                elif line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        rss = float(parts[1])
        return (utime + stime, last_cpu, allowed, command, rss or 0.0)
    except (OSError, IndexError, ValueError):
        return None
