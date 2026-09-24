"""CPU_Checker の CLI。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cpu_checker.collect import sample_proc, write_samples
from cpu_checker.parsers.launch_log import list_launch_logs
from cpu_checker.report import build_report

DEFAULT_LAUNCH = str(Path.home() / ".ros" / "log")


def _optional_launch_paths(raw: str) -> list[Path]:
    """`--launch` を launch.log のリストに直す。省略時の場所に無ければ空。"""
    try:
        return list_launch_logs(Path(raw))
    except FileNotFoundError:
        if raw == DEFAULT_LAUNCH:
            return []
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cpu_checker",
        description="実行中マシンの CPU 使用状況を可視化する。ROS 2 の Node ↔ PID があれば名前を付ける",
    )
    sub = parser.add_subparsers(dest="command")

    report = sub.add_parser("report", help="サンプル (+ 任意の launch ログ) から HTML を出す")
    report.add_argument(
        "--launch",
        default=DEFAULT_LAUNCH,
        help="公式 launch.log、またはそれが入ったディレクトリ（中の全 launch.log を時刻順にマージ。省略時は ~/.ros/log）",
    )
    report.add_argument("--top", help="top バッチログ、または collect の CSV")
    report.add_argument("--out", default="output/report.html", help="HTML 出力先")
    report.add_argument(
        "--ros-only",
        action="store_true",
        help="launch ログに載った PID だけ出す（既定は全プロセス）",
    )
    report.add_argument("--top-n", type=int, default=15, help="時系列・ランキングに出す上位件数")

    collect = sub.add_parser("collect", help="実行中の全プロセスを /proc からサンプリングする")
    collect.add_argument("--interval", type=float, default=1.0, help="間隔（秒）")
    collect.add_argument("--duration", type=float, help="長さ（秒）。省略時は Ctrl+C まで")
    collect.add_argument("--out", default="output/samples.csv")

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "report":
        launch_paths = _optional_launch_paths(args.launch)
        if not launch_paths:
            print("launch=(none)")
        elif len(launch_paths) == 1:
            print(f"launch={launch_paths[0]}")
        else:
            print(f"launch={len(launch_paths)} files")
            for item in launch_paths:
                print(f"  {item}")
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        groups = build_report(
            launch_paths=launch_paths,
            top_path=Path(args.top) if args.top else None,
            out_html=out,
            include_unmapped=not args.ros_only,
            top_n=args.top_n,
        )
        print(f"wrote {out} ({len(groups)} PIDs)")
        print(f"wrote {out.parent / 'pid_cpu.csv'}")
        print(f"wrote {out.parent / 'container_members.csv'}")
        return 0

    if args.command == "collect":
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        print("sampling all /proc PIDs")
        samples = sample_proc(interval_sec=args.interval, duration_sec=args.duration)
        write_samples(samples, out)
        print(f"wrote {out} ({len(samples)} samples)")
        return 0

    print(f"unknown command: {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
