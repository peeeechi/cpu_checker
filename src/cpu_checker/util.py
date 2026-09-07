"""表示名と数値の共通処理。"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime

from cpu_checker.models import ProcessSample


def logger_to_path(logger: str) -> str:
    if logger.startswith("/"):
        return logger
    return "/" + logger.replace(".", "/")


def path_to_logger(path: str) -> str:
    return path.lstrip("/").replace("/", ".")


def names_match_container(logger: str, container: str) -> bool:
    if logger == container or logger == container.lstrip("/"):
        return True
    return path_to_logger(container) == logger or logger_to_path(logger) == container


def parse_res_kib(raw: str) -> float | None:
    if raw is None or raw == "":
        return None
    text = str(raw).strip().lower()
    try:
        if text.endswith("g"):
            return float(text[:-1]) * 1.049e6
        if text.endswith("m"):
            return float(text[:-1]) * 1024
        return float(text)
    except ValueError:
        return None


def parse_affinity_cores(allowed: str | None) -> list[int]:
    if not allowed or allowed in ("-", ""):
        return []
    cores: list[int] = []
    for part in allowed.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            cores.extend(range(int(start_s), int(end_s) + 1))
        else:
            cores.append(int(part))
    return cores


def short_node_name(name: str) -> str:
    return name.rstrip("/").split("/")[-1] or name


def format_display_names(names: list[str], max_show: int = 2, short: bool = False) -> str:
    if not names:
        return "(unknown)"
    shown = [short_node_name(n) if short else n for n in names]
    if len(shown) <= max_show:
        return ", ".join(shown)
    return f"{', '.join(shown[:max_show])} … ({len(shown)} nodes)"


def mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


CORE_DISPLAY_LIMIT = 3
CORE_CAP = 100.0


def per_core_cpu(
    samples: list[ProcessSample],
    cap: float | None = CORE_CAP,
) -> dict[int | None, list[float]]:
    """サンプルをコア番号に載せる。コア不明は None。1 コア表示は cap で頭打ち。"""
    buckets: dict[int | None, list[float]] = {}
    for sample in samples:
        core: int | None = sample.last_cpu
        if core is None:
            cores = parse_affinity_cores(sample.cpu_allowed)
            if len(cores) == 1:
                core = cores[0]
        value = sample.cpu_percent
        if cap is not None and core is not None:
            value = min(value, cap)
        buckets.setdefault(core, []).append(value)
    return buckets


def format_core_map(
    buckets: dict[int | None, list[float]],
    reducer,
    limit: int = CORE_DISPLAY_LIMIT,
) -> str:
    if not buckets:
        return "-"
    ranked = sorted(
        buckets.items(),
        key=lambda item: reducer(item[1]),
        reverse=True,
    )
    parts: list[str] = []
    for core, values in ranked[:limit]:
        label = "不明" if core is None else f"CPU{core}"
        parts.append(f"{label} {reducer(values):.0f}%")
    extra = len(ranked) - limit
    if extra > 0:
        parts.append(f"他{extra}コア")
    return ", ".join(parts)


def format_core_range(cores: list[int]) -> str:
    if not cores:
        return "-"
    unique = sorted(set(cores))
    ranges: list[str] = []
    start = prev = unique[0]
    for core in unique[1:]:
        if core == prev + 1:
            prev = core
            continue
        ranges.append(f"{start}-{prev}" if start != prev else str(start))
        start = prev = core
    ranges.append(f"{start}-{prev}" if start != prev else str(start))
    return ",".join(ranges)


def affinity_label(samples: list[ProcessSample]) -> str:
    allowed = {s.cpu_allowed for s in samples if s.cpu_allowed}
    if len(allowed) == 1:
        return next(iter(allowed))
    cores = parse_affinity_cores(next(iter(allowed))) if allowed else []
    if cores:
        return format_core_range(cores)
    return "-"


def used_core_label(buckets: dict[int | None, list[float]]) -> str:
    cores = [core for core in buckets if core is not None]
    if not cores:
        return "不明"
    return format_core_range(cores)


def most_common_core(samples: list[ProcessSample]) -> str:
    cores: list[int] = []
    for sample in samples:
        if sample.last_cpu is not None:
            cores.append(sample.last_cpu)
            continue
        allowed = parse_affinity_cores(sample.cpu_allowed)
        if len(allowed) == 1:
            cores.append(allowed[0])
    if cores:
        core, _ = Counter(cores).most_common(1)[0]
        return f"CPU{core}"
    allowed_sets = {s.cpu_allowed for s in samples if s.cpu_allowed}
    if len(allowed_sets) == 1:
        return next(iter(allowed_sets))
    return "-"


def saturated_cores(buckets: dict[int | None, list[float]], threshold: float = 100.0) -> list[str]:
    found: list[str] = []
    for core, values in buckets.items():
        if core is None:
            continue
        if max(values) >= threshold:
            found.append(f"CPU{core}")
    return found


def parse_iso_or_time(raw: str, date_hint: datetime | None) -> datetime:
    raw = raw.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    if re.fullmatch(r"\d{1,2}:\d{2}:\d{2}", raw):
        base = date_hint.date() if date_hint else datetime.now().date()
        parsed = datetime.strptime(raw, "%H:%M:%S")
        return datetime.combine(base, parsed.time())
    raise ValueError(f"unsupported timestamp: {raw}")
