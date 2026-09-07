"""CPU_Checker のデータ契約。パーサ・結合・可視化はこの型を受け渡す。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class NodeKind(str, Enum):
    STANDALONE = "standalone"  # 1 Node = 1 プロセス
    COMPOSABLE = "composable"  # コンテナ 1 プロセスに複数 Node


@dataclass(frozen=True)
class LaunchSession:
    """1 回の ros2 launch 起動。公式ログのセッションディレクトリから取る。"""

    started_at: datetime | None = None  # 起動日時（ディレクトリ名の 2026-09-03-16-34-34）
    ros_log_dir: str | None = None  # ~/.ros/log/<日付-ホスト-PID>
    host: str | None = None  # 起動したマシン名（例: npc2405018）


@dataclass(frozen=True)
class NodeProcess:
    """公式ログから得た Node ↔ PID の 1 行。"""

    node_name: str  # 表示用の ROS Node 名（例: /ad_sound_manager）
    kind: NodeKind  # standalone か composable
    pid: int  # OS のプロセス番号。コンテナ未特定は -1
    launch_name: str  # launch が付けた実行ファイル名（例: component_container_mt）
    launch_index: int  # launch の通し番号（同名実行ファイルを分ける。未特定は -1）
    container_name: str | None = None  # 載っているコンテナ名。standalone は None
    ros_logger: str | None = None  # ログ上のロガー名（. 区切り。例: control.control_container）


@dataclass(frozen=True)
class ProcessSample:
    """top または /proc の 1 サンプル。1 PID × 1 時刻。"""

    timestamp: datetime  # 取った時刻
    pid: int  # 対象プロセス
    cpu_percent: float  # プロセス全体の %CPU（1 コア = 100%）
    command: str  # 実行名（表示用。結合には使わない）
    res_kib: float | None = None  # 常駐メモリ VmRSS（KiB）
    last_cpu: int | None = None  # 最後に走ったコア番号（/proc の Psr）。top には無い
    cpu_allowed: str | None = None  # 許可コア（Cpus_allowed_list。例: 0-19）


@dataclass
class EnrichedRecord:
    """Node 1 件に時系列を付けたもの。同じ pid を複数 Node が共有しうる。"""

    node: NodeProcess  # 所属 Node
    samples: list[ProcessSample] = field(default_factory=list)  # その PID の時系列


@dataclass
class PidGroup:
    """可視化の 1 単位。キーは pid。表示名は所属 Node 名。"""

    pid: int  # このグループのプロセス番号
    members: list[NodeProcess] = field(default_factory=list)  # この PID に載っている Node
    samples: list[ProcessSample] = field(default_factory=list)  # この PID の %CPU 時系列
    container_name: str | None = None  # composable ならコンテナ名。それ以外は None

    @property
    def display_names(self) -> list[str]:
        names = [m.node_name for m in self.members]
        if names:
            return names
        if self.samples:
            return [self.samples[0].command]
        return [f"pid:{self.pid}"]

    @property
    def is_composable(self) -> bool:
        return any(m.kind == NodeKind.COMPOSABLE for m in self.members)

    @property
    def kind_label(self) -> str:
        if self.is_composable:
            return "composable"
        if self.members:
            return "standalone"
        return "unmapped"

    @property
    def mean_cpu(self) -> float:
        if not self.samples:
            return 0.0
        return sum(s.cpu_percent for s in self.samples) / len(self.samples)

    @property
    def max_cpu(self) -> float:
        if not self.samples:
            return 0.0
        return max(s.cpu_percent for s in self.samples)
