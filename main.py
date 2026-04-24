#!/usr/bin/env python3
"""
Radiology Command Center — CLI entry point.

Usage
-----
  python main.py run   --input data.xlsx [--historical data.xlsx] [--output reports/]
  python main.py template --output template.xlsx
  python main.py demo  [--output reports/]

Commands
--------
  run       Process an Excel workbook and produce all outputs.
  template  Generate a blank Excel template with correct headers.
  demo      Run on synthetic data — useful for testing without real data.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── Pipeline ─────────────────────────────────────────────────────────────────

def run_pipeline(
    input_path: str,
    historical_path: str | None,
    output_dir: str,
    now: datetime | None = None,
) -> None:
    """Full analysis pipeline: load → track → predict → prioritise → RCA → report."""
    from radiology_command_center.data_loader import (
        load_cases_from_excel,
        load_historical_from_excel,
    )
    from radiology_command_center.prediction_engine import PredictionEngine
    from radiology_command_center.prioritization_engine import PrioritizationEngine
    from radiology_command_center.rca_engine import RCAEngine
    from radiology_command_center.report_generator import ReportGenerator
    from radiology_command_center.workflow_tracker import WorkflowTracker

    if now is None:
        now = datetime.now()

    print(f"\n{'═' * 68}")
    print("  RADIOLOGY COMMAND CENTER")
    print(f"  {now.strftime('%A %B %d, %Y  %H:%M')}")
    print(f"{'═' * 68}\n")

    # 1. Load data
    print("► Loading cases …")
    cases, warnings = load_cases_from_excel(input_path)
    for w in warnings:
        logger.warning(w)
    print(f"  {len(cases)} cases loaded from {input_path}")

    historical = []
    if historical_path:
        historical, hw = load_historical_from_excel(historical_path)
        for w in hw:
            logger.warning(w)
        print(f"  {len(historical)} historical cases loaded for baseline")

    # 2. Workflow tracking
    print("\n► Tracking workflow stages …")
    tracker = WorkflowTracker(reference_time=now)
    cases = tracker.process(cases)
    historical = tracker.process(historical)

    stuck = sum(1 for c in cases if c.is_stuck)
    print(f"  Stuck cases: {stuck}")

    # 3. Prediction
    print("\n► Scoring SLA breach risk …")
    predictor = PredictionEngine(historical_cases=historical)
    cases = predictor.score(cases)

    at_risk = predictor.cases_at_risk(cases)
    print(f"  At-risk cases (≥45%): {len(at_risk)}")
    critical = [c for c in cases if c.breach_risk_score >= 0.85]
    print(f"  Critical breach risk: {len(critical)}")

    # 4. Prioritization
    print("\n► Ranking worklist …")
    prioritiser = PrioritizationEngine()
    cases = prioritiser.rank(cases)

    result = prioritiser.next_best_case(cases)
    if result:
        top_case, rec = result
        print(f"\n  ┌─ NEXT BEST CASE ─────────────────────────────────────────")
        print(f"  │  {rec}")
        print(f"  └──────────────────────────────────────────────────────────\n")

    # 5. RCA
    print("► Running root cause analysis …")
    rca = RCAEngine()
    findings, bottlenecks = rca.analyse(cases, historical)
    top_drivers = rca.top_drivers(findings, n=3)

    if top_drivers:
        print("  Top delay drivers:")
        for i, f in enumerate(top_drivers, 1):
            print(f"    {i}. [{f.category}] {f.value}: {f.deviation_percent:+.0f}% vs baseline")

    if bottlenecks:
        print("  Bottleneck stages:")
        for b in bottlenecks[:3]:
            print(f"    [{b.severity}] {b.stage}: +{b.avg_delay_minutes:.0f} min avg delay")

    # 6. Report generation
    print(f"\n► Generating reports → {output_dir}/")
    reporter = ReportGenerator(output_dir=output_dir)
    report = reporter.run(
        cases,
        historical=historical,
        rca_findings=findings,
        bottlenecks=bottlenecks,
        now=now,
    )

    # 7. Print executive summary to stdout
    print("\n" + report.executive_summary)

    print(f"\n✓ Done. Reports written to: {output_dir}/\n")


# ── Template generation ───────────────────────────────────────────────────────

def generate_template(output_path: str) -> None:
    from radiology_command_center.data_loader import generate_excel_template

    generate_excel_template(output_path)
    print(f"✓ Template written: {output_path}")
    print(
        "\nFill in the 'Cases' sheet with today's active cases.\n"
        "Use the 'Historical' sheet for completed cases (for baseline calculation).\n"
        "Then run:  python main.py run --input <your_file>.xlsx\n"
    )


# ── Demo mode ────────────────────────────────────────────────────────────────

def run_demo(output_dir: str) -> None:
    import tempfile
    import os
    from sample_data_generator import generate_sample_workbook

    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        print("► Generating synthetic demo data …")
        generate_sample_workbook(tmp_path)
        run_pipeline(
            input_path=tmp_path,
            historical_path=tmp_path,
            output_dir=output_dir,
        )
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="radiology-command-center",
        description="Radiology Command Center — real-time operational intelligence",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # run
    run_p = sub.add_parser("run", help="Process Excel input and generate reports")
    run_p.add_argument("--input",       required=True, help="Path to Cases Excel file")
    run_p.add_argument("--historical",  default=None,  help="Path to Historical Excel file (optional)")
    run_p.add_argument("--output",      default="reports", help="Output directory (default: reports/)")
    run_p.add_argument("--as-of",       default=None,
                       help="Reference datetime ISO8601 (default: now); useful for replaying historical snapshots")

    # template
    tmpl_p = sub.add_parser("template", help="Generate a blank input template")
    tmpl_p.add_argument("--output", default="radiology_template.xlsx", help="Output path")

    # demo
    demo_p = sub.add_parser("demo", help="Run with synthetic data (no input file needed)")
    demo_p.add_argument("--output", default="reports", help="Output directory")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "run":
        now = None
        if args.as_of:
            try:
                now = datetime.fromisoformat(args.as_of)
            except ValueError:
                print(f"ERROR: --as-of must be ISO8601 format, e.g. '2024-01-15T09:00:00'")
                sys.exit(1)
        run_pipeline(args.input, args.historical, args.output, now=now)

    elif args.command == "template":
        generate_template(args.output)

    elif args.command == "demo":
        run_demo(args.output)


if __name__ == "__main__":
    main()
