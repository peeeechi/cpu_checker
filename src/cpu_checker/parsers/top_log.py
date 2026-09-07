"""top バッチログまたは CSV からプロセスサンプルを取る。"""

from __future__ import annotations

import csv
import re
from datetime import datetime, timedelta
from pathlib import Path

from cpu_checker.models import ProcessSample
from cpu_checker.util import parse_iso_or_time, parse_res_kib

RE_TOP_TS = re.compile(r"top - (\d{1,2}:\d{2}:\d{2})")
DEFAULT_COLS = [
    "PID",
    "USER",
    "PR",
    "NI",
    "VIRT",
    "RES",
    "SHR",
    "S",
    "%CPU",
    "%MEM",
    "TIME+",
    "COMMAND",
]


def parse_top_log(path: Path, date_hint: datetime | None = None) -> list[ProcessSample]:
    """テキストなら log2csv 相当で CSV 化し、PID / %CPU / RES / Psr を読む。"""
    sample = path.read_text(encoding="utf-8", errors="replace")[:4096]
    if _looks_like_csv(sample):
        return _parse_csv(path, date_hint)
    return _parse_top_text(path, date_hint)


def _looks_like_csv(head: str) -> bool:
    first = next((line.strip() for line in head.splitlines() if line.strip()), "")
    if not first:
        return False
    if first.startswith("top -"):
        return False
    lower = first.lower()
    return ("pid" in lower and ("%cpu" in lower or "cpu" in lower)) or first.count(",") >= 3


def _parse_csv(path: Path, date_hint: datetime | None) -> list[ProcessSample]:
    samples: list[ProcessSample] = []
    with path.open(encoding="utf-8", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            return []
        fields = {name.strip(): name for name in reader.fieldnames}
        pid_key = _pick(fields, "PID", "pid")
        cpu_key = _pick(fields, "%CPU", "CPU", "pcpu", "cpu_percent")
        ts_key = _pick(fields, "timestamp", "time", "datetime")
        cmd_key = _pick(fields, "COMMAND", "command", "CMD", "comm")
        res_key = _pick(fields, "RES", "res", "RSS", "rss")
        psr_key = _pick(fields, "Psr", "psr", "last_cpu", "LastCPU")
        aff_key = _pick(fields, "Cpus_allowed", "cpu_allowed", "affinity")
        for row in reader:
            if not pid_key or not cpu_key or pid_key not in row:
                continue
            pid_raw = (row.get(pid_key) or "").strip()
            cpu_raw = (row.get(cpu_key) or "").strip()
            if not pid_raw or not cpu_raw:
                continue
            ts_raw = (row.get(ts_key) or "").strip() if ts_key else ""
            timestamp = parse_iso_or_time(ts_raw, date_hint) if ts_raw else (
                date_hint or datetime.now()
            )
            last_cpu = _to_int(row.get(psr_key)) if psr_key else None
            allowed = (row.get(aff_key) or "").strip() if aff_key else None
            samples.append(
                ProcessSample(
                    timestamp=timestamp,
                    pid=int(float(pid_raw)),
                    cpu_percent=float(cpu_raw),
                    command=(row.get(cmd_key) or "").strip() if cmd_key else "",
                    res_kib=parse_res_kib(row.get(res_key, "")) if res_key else None,
                    last_cpu=last_cpu,
                    cpu_allowed=allowed or None,
                )
            )
    return _fix_midnight(samples)


def _parse_top_text(path: Path, date_hint: datetime | None) -> list[ProcessSample]:
    samples: list[ProcessSample] = []
    current_ts: datetime | None = None
    headers = list(DEFAULT_COLS)
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        ts_m = RE_TOP_TS.search(line)
        if ts_m:
            current_ts = parse_iso_or_time(ts_m.group(1), date_hint)
            continue
        tokens = line.split()
        if tokens and tokens[0] == "PID":
            headers = tokens
            continue
        if current_ts is None:
            continue
        if not tokens or not tokens[0].isdigit():
            continue
        mapped = _row_from_tokens(tokens, headers)
        if mapped is None:
            continue
        samples.append(
            ProcessSample(
                timestamp=current_ts,
                pid=int(mapped["PID"]),
                cpu_percent=float(mapped["%CPU"]),
                command=mapped.get("COMMAND", ""),
                res_kib=parse_res_kib(mapped.get("RES", "")),
                last_cpu=_to_int(mapped.get("Psr") or mapped.get("PSR")),
                cpu_allowed=mapped.get("Cpus_allowed") or None,
            )
        )
    return _fix_midnight(samples)


def _row_from_tokens(tokens: list[str], headers: list[str]) -> dict[str, str] | None:
    if len(tokens) < 2:
        return None
    extra = max(0, len(tokens) - len(headers))
    values = list(tokens)
    if extra > 0 and headers and headers[-1] == "COMMAND":
        values = tokens[: len(headers) - 1] + [" ".join(tokens[len(headers) - 1 :])]
    if len(values) < len(headers):
        if len(values) < 9:
            return None
        # 列が足りないときは先頭から DEFAULT で埋める
        use = DEFAULT_COLS[: len(values)]
        mapped = dict(zip(use, values))
    else:
        mapped = dict(zip(headers, values[: len(headers)]))
    if "PID" not in mapped or "%CPU" not in mapped:
        return None
    return mapped


def _pick(fields: dict[str, str], *candidates: str) -> str | None:
    lower = {key.lower(): orig for key, orig in fields.items()}
    for name in candidates:
        if name in fields:
            return fields[name]
        if name.lower() in lower:
            return lower[name.lower()]
    return None


def _to_int(raw: str | None) -> int | None:
    if raw is None or str(raw).strip() in ("", "-"):
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def _fix_midnight(samples: list[ProcessSample]) -> list[ProcessSample]:
    if not samples:
        return samples
    fixed: list[ProcessSample] = []
    offset = timedelta()
    prev: datetime | None = None
    for sample in samples:
        ts = sample.timestamp + offset
        if prev is not None and ts < prev - timedelta(hours=1):
            offset += timedelta(days=1)
            ts = sample.timestamp + offset
        prev = ts
        if ts != sample.timestamp:
            sample = ProcessSample(
                timestamp=ts,
                pid=sample.pid,
                cpu_percent=sample.cpu_percent,
                command=sample.command,
                res_kib=sample.res_kib,
                last_cpu=sample.last_cpu,
                cpu_allowed=sample.cpu_allowed,
            )
        fixed.append(sample)
    return fixed
