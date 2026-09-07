"""PidGroup から HTML レポートを書く。"""

from __future__ import annotations

from datetime import datetime
from html import escape
from pathlib import Path

import plotly.graph_objects as go

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


def _group_label(group: PidGroup, max_show: int = 1) -> str:
    if group.container_name:
        return f"{short_node_name(group.container_name)} ({len(group.display_names)} nodes)"
    return format_display_names(group.display_names, max_show, short=True)

TOP_N = 15
SAT_THRESHOLD = 100.0


def write_report(
    groups: list[PidGroup],
    out: Path,
    session: LaunchSession | None = None,
    unmapped: list[NodeProcess] | None = None,
    top_n: int = TOP_N,
    source_note: str = "",
) -> None:
    ranked = _rank_groups(groups)
    heavy = [g for g in ranked if g.samples][:top_n]
    parts = [
        _summary_html(ranked, session, source_note),
        _pid_table_html(ranked),
        _ranking_bar(heavy).to_html(full_html=False, include_plotlyjs="cdn", config={"displaylogo": False}),
        _timeseries(heavy).to_html(full_html=False, include_plotlyjs=False, config={"displaylogo": False}),
    ]
    heatmap = _heatmap(ranked)
    if heatmap is not None:
        parts.append(heatmap.to_html(full_html=False, include_plotlyjs=False, config={"displaylogo": False}))
    parts.append(_members_table_html(ranked, unmapped or []))

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_wrap_html(parts), encoding="utf-8")


def _rank_groups(groups: list[PidGroup]) -> list[PidGroup]:
    return sorted(groups, key=lambda g: (g.mean_cpu, g.max_cpu), reverse=True)


def _summary_html(
    groups: list[PidGroup],
    session: LaunchSession | None,
    source_note: str,
) -> str:
    node_count = sum(len(g.members) for g in groups)
    container_count = len({g.container_name for g in groups if g.container_name})
    sample_count = sum(len(g.samples) for g in groups)
    has_core = any(s.last_cpu is not None for g in groups for s in g.samples)
    started = session.started_at.strftime("%Y-%m-%d %H:%M:%S") if session and session.started_at else "-"

    top_items = []
    for group in [g for g in groups if g.samples][:3]:
        top_items.append(
            f"<li><strong>{escape(_group_label(group))}</strong>"
            f" (PID {group.pid}) 平均 {group.mean_cpu:.0f}% / 最大 {group.max_cpu:.0f}%</li>"
        )
    top_html = f"<ol>{''.join(top_items)}</ol>" if top_items else "<p>-</p>"

    sat_rows: list[str] = []
    for group in groups:
        sat = saturated_cores(per_core_cpu(group.samples), SAT_THRESHOLD)
        if sat:
            sat_rows.append(
                f"<li><strong>{', '.join(sat)}</strong> — "
                f"{escape(_group_label(group))} (PID {group.pid})</li>"
            )

    warn = ""
    if sat_rows:
        warn = (
            "<div class='warn'><strong>飽和の疑い (コア割り当て後 100%)</strong>"
            " — プロセス全体の %CPU を最終実行コアに載せているので、瞬間値は過大になりやすい。"
            f"<ul>{''.join(sat_rows)}</ul></div>"
        )
    elif sample_count and not has_core:
        warn = (
            "<div class='note'><strong>コア不明</strong> — 最終実行コアがありません。"
            "PID 合計の %CPU は出せますが、どのコアが 100% かは分かりません。</div>"
        )
    elif sample_count == 0:
        warn = (
            "<div class='note'><strong>CPU サンプルなし</strong> — launch ログだけの実行です。"
            "使用率を見るには <code>--top</code> か <code>collect</code> を使ってください。</div>"
        )

    return f"""
    <section class="summary">
      <h1>CPU_Checker レポート</h1>
      <p class="meta">起動: {escape(started)} / プロセス: {len(groups)} / Node: {node_count} /
      コンテナ: {container_count} / サンプル: {sample_count}</p>
      <p class="meta">プロセスの %CPU は 1 コア = 100%（200% = 2 コア分）。順位は平均。データ源: {escape(source_note or '-')}</p>
      <h2>平均が高いプロセス</h2>
      {top_html}
      {warn}
    </section>
    """


def _pid_table_html(groups: list[PidGroup]) -> str:
    body_rows: list[str] = []
    for group in groups:
        buckets = per_core_cpu(group.samples)
        sat = ", ".join(saturated_cores(buckets, SAT_THRESHOLD)) or "-"
        names = "<br>".join(escape(n) for n in group.display_names[:4])
        if len(group.display_names) > 4:
            names += f"<br>… 他 {len(group.display_names) - 4} Node"
        mean_txt = f"{group.mean_cpu:.1f}" if group.samples else "-"
        max_txt = f"{group.max_cpu:.1f}" if group.samples else "-"
        body_rows.append(
            "<tr>"
            f"<td class='name'>{names}</td>"
            f"<td>{group.pid}</td>"
            f"<td>{escape(group.kind_label)}</td>"
            f"<td class='num'>{mean_txt}</td>"
            f"<td class='num'>{max_txt}</td>"
            f"<td>{escape(affinity_label(group.samples) if group.samples else '-')}</td>"
            f"<td>{escape(used_core_label(buckets) if group.samples else '-')}</td>"
            f"<td>{escape(format_core_map(buckets, max) if group.samples else '-')}</td>"
            f"<td>{escape(sat)}</td>"
            f"<td class='num'>{len(group.samples)}</td>"
            "</tr>"
        )
    return f"""
    <section class="panel">
      <h2>PID × CPU 使用率</h2>
      <p class="meta">平均 / 最大は<strong>プロセス全体</strong>（1 コア満載 = 100%。2 コアぶん使えば 200%）。
      ピン留めしていなければ、1 プロセスは複数コアを渡り歩く。
      許可コア = 走ってよい集合。使用コア = 実際に居たコア全部。負荷上位 = そのうち忙しい 3 コア。</p>
      <div class="scroll">
        <table class="data">
          <thead><tr>
            <th>Node</th><th>PID</th><th>種別</th>
            <th>平均%</th><th>最大%</th>
            <th>許可コア</th><th>使用コア</th><th>負荷上位</th><th>飽和</th><th>n</th>
          </tr></thead>
          <tbody>{''.join(body_rows)}</tbody>
        </table>
      </div>
    </section>
    """


def _ranking_bar(groups: list[PidGroup]) -> go.Figure:
    names: list[str] = []
    means: list[float] = []
    maxes: list[float] = []
    for group in reversed(groups):
        names.append(f"{_group_label(group)} [{group.pid}]")
        means.append(group.mean_cpu)
        maxes.append(group.max_cpu)
    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            name="平均",
            x=means,
            y=names,
            orientation="h",
            marker_color="#2980b9",
            hovertemplate="%{y}<br>平均 %{x:.1f}%<extra></extra>",
        )
    )
    fig.add_trace(
        go.Bar(
            name="最大",
            x=maxes,
            y=names,
            orientation="h",
            marker_color="#e67e22",
            hovertemplate="%{y}<br>最大 %{x:.1f}%<extra></extra>",
        )
    )
    fig.update_layout(
        title="圧迫ランキング（平均順。最大は参考）",
        xaxis_title="%CPU（プロセス全体。1 コア = 100%）",
        barmode="group",
        height=max(400, 36 * max(len(groups), 1) + 140),
        margin=dict(l=280, r=40, t=60, b=40),
        legend=dict(orientation="h", y=1.08),
        font=dict(size=13),
    )
    return fig


def _timeseries(groups: list[PidGroup]) -> go.Figure:
    fig = go.Figure()
    for group in groups:
        if not group.samples:
            continue
        first = group.samples[0].timestamp
        xs = [(s.timestamp - first).total_seconds() for s in group.samples]
        ys = [s.cpu_percent for s in group.samples]
        cores = ["" if s.last_cpu is None else f"CPU{s.last_cpu}" for s in group.samples]
        fig.add_trace(
            go.Scatter(
                x=xs,
                y=ys,
                mode="lines",
                name=_group_label(group),
                hovertemplate=(
                    "PID %{text}<br>%{x:.0f}s<br>%{y:.1f}% %{customdata}<extra></extra>"
                ),
                text=[str(group.pid)] * len(xs),
                customdata=cores,
            )
        )
    fig.update_layout(
        title="PID ごとの %CPU 時系列（プロセス全体）",
        xaxis_title="経過秒",
        yaxis_title="%CPU",
        height=480,
        legend=dict(orientation="h", y=-0.25),
        margin=dict(b=80),
        font=dict(size=13),
    )
    if not groups or all(not g.samples for g in groups):
        fig.add_annotation(text="サンプルがありません", showarrow=False, xref="paper", yref="paper", x=0.5, y=0.5)
    return fig


def _heatmap(groups: list[PidGroup]) -> go.Figure | None:
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
    fig = go.Figure(
        go.Heatmap(
            x=times,
            y=[f"CPU{c}" for c in cores],
            z=z,
            text=text,
            hovertemplate="t=%{x}s<br>%{y}<br>%{z:.0f}%<br>%{text}<extra></extra>",
            colorscale="YlOrRd",
            zmin=0,
            zmax=100,
            colorbar=dict(title="%"),
        )
    )
    fig.update_layout(
        title="コア別ヒートマップ（割り当て後。色は 0〜100%）",
        height=360 + 22 * len(cores),
        font=dict(size=12),
    )
    return fig


def _members_table_html(groups: list[PidGroup], unmapped: list[NodeProcess]) -> str:
    rows: list[str] = []
    for group in groups:
        if not group.is_composable:
            continue
        mean_txt = f"{group.mean_cpu:.1f}" if group.samples else "-"
        max_txt = f"{group.max_cpu:.1f}" if group.samples else "-"
        members = group.members or []
        for index, member in enumerate(members):
            pid_cell = str(group.pid) if index == 0 else ""
            mean_cell = mean_txt if index == 0 else ""
            max_cell = max_txt if index == 0 else ""
            container = group.container_name or "-"
            rows.append(
                "<tr>"
                f"<td class='name'>{escape(member.node_name)}</td>"
                f"<td>{pid_cell}</td>"
                f"<td class='name'>{escape(container)}</td>"
                f"<td class='num'>{mean_cell}</td>"
                f"<td class='num'>{max_cell}</td>"
                "</tr>"
            )
    for node in unmapped:
        rows.append(
            "<tr>"
            f"<td class='name'>{escape(node.node_name)}</td>"
            "<td>-</td>"
            f"<td class='name'>{escape(node.container_name or '-')}</td>"
            "<td colspan='2'>PID 未特定</td>"
            "</tr>"
        )
    return f"""
    <section class="panel">
      <h2>Composable 所属表</h2>
      <p class="meta">Composable の箱の中身と、PID が決まらなかった Node だけ。
      同じ PID の平均 / 最大は箱全体。中の Node ごとの内訳ではない。</p>
      <div class="scroll">
        <table class="data">
          <thead><tr>
            <th>所属 Node</th><th>PID</th><th>コンテナ</th><th>平均%</th><th>最大%</th>
          </tr></thead>
          <tbody>{''.join(rows)}</tbody>
        </table>
      </div>
    </section>
    """


def _wrap_html(parts: list[str]) -> str:
    body = "\n".join(parts)
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="utf-8"/>
  <title>CPU_Checker レポート</title>
  <style>
    body {{ font-family: sans-serif; margin: 24px; background: #f4f5f7; color: #222; }}
    h1 {{ margin: 0 0 0.4em; font-size: 1.6rem; }}
    h2 {{ margin: 0 0 0.4em; font-size: 1.15rem; }}
    .summary, .panel {{ background: #fff; padding: 16px 20px; border-radius: 8px; margin-bottom: 16px;
      box-shadow: 0 1px 2px rgba(0,0,0,.06); }}
    .meta {{ color: #555; font-size: 0.92rem; }}
    .warn {{ background: #fdecea; border-left: 4px solid #c0392b; padding: 10px 14px; }}
    .note {{ background: #eaf2f8; border-left: 4px solid #2980b9; padding: 10px 14px; }}
    .scroll {{ max-height: 70vh; overflow: auto; border: 1px solid #e0e0e0; }}
    table.data {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    table.data th {{ position: sticky; top: 0; background: #1f4e79; color: #fff; text-align: left;
      padding: 8px 10px; white-space: nowrap; z-index: 1; }}
    table.data td {{ padding: 6px 10px; border-bottom: 1px solid #eee; vertical-align: top; }}
    table.data tr:nth-child(even) {{ background: #fafafa; }}
    table.data td.name {{ max-width: 360px; word-break: break-all; }}
    table.data td.num {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
  </style>
</head>
<body>
{body}
</body>
</html>
"""
