"""Concise error / data-quality report (Markdown or JSON) built from the run ledger and checks."""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from typing import Any

import psycopg

from .db import queries


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join("" if v is None else str(v).replace("|", "\\|") for v in row) + " |" for row in rows]
    return "\n".join(out)


def build_report(conn: psycopg.Connection[Any], run_id: str | None = None) -> dict[str, Any]:
    run = None
    if run_id:
        run = next((r for r in queries.runs(conn, 200) if str(r["run_id"]) == run_id), None)
    else:
        run = queries.latest_run(conn)
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "run": run,
        "runs": queries.runs(conn, 10),
        "summary": queries.summary(conn),
        "corpus": queries.corpus_stats(conn),
        "checks": [c.__dict__ for c in queries.run_checks(conn)],
        "issues": queries.run_issues(conn, str(run["run_id"]), limit=200) if run else [],
        "unresolved": queries.list_signals(conn, queries.SignalFilter(), limit=500)[0],
    }


def render_markdown(data: dict[str, Any]) -> str:
    s, run = data["summary"], data["run"]
    lines = [
        "# IRIS-SIG-45 — Error & data-quality report",
        "",
        f"Generated {data['generated_at']} · source `jll_de_newsroom` · target `iris_staging.signal_ingest`",
        "",
        "## Reported run",
    ]
    if run:
        stats = run.get("stats") or {}
        counters = stats.get("counters", {})
        lines += [
            "",
            _table(
                ["run_id", "mode", "status", "started", "duration (s)", "errors", "warnings"],
                [
                    [
                        str(run["run_id"])[:8],
                        run["mode"],
                        run["status"],
                        run["started_at"].isoformat(timespec="seconds"),
                        stats.get("duration_seconds"),
                        run["errors"],
                        run["warnings"],
                    ]
                ],
            ),
            "",
            "Counters: " + ", ".join(f"`{k}`={v}" for k, v in sorted(counters.items())) if counters else "Counters: —",
            "",
        ]
        check = run.get("source_check") or {}
        if check.get("redirect_chain") is not None:
            chain = " → ".join(f"{h['status']} {h['url']}" for h in check["redirect_chain"]) or "(none)"
            lines += [
                "**Source verification** (performed at run time, not assumed):",
                "",
                f"- brief endpoint `{check.get('brief_endpoint')}`: {chain} → **{check.get('final_status')}** `{check.get('final_url')}`",
                f"- robots.txt forbids query strings (listing pagination): **{(check.get('robots') or {}).get('disallow_query_strings')}**; sitemap allowed: **{(check.get('robots') or {}).get('sitemap_allowed')}**",
                "",
            ]
    else:
        lines += ["", "_No run recorded yet._", ""]

    if data.get("runs"):
        lines += [
            "## Run history",
            "",
            _table(
                ["run_id", "mode", "status", "started", "duration (s)", "counters", "error"],
                [
                    [
                        str(r["run_id"])[:8],
                        r["mode"],
                        r["status"],
                        r["started_at"].isoformat(timespec="seconds"),
                        (r.get("stats") or {}).get("duration_seconds"),
                        ", ".join(
                            f"{k}={v}" for k, v in sorted(((r.get("stats") or {}).get("counters") or {}).items())
                        ),
                        (r.get("error") or "")[:110],
                    ]
                    for r in data["runs"]
                ],
            ),
            "",
        ]
    lines += [
        "## Staged signals",
        "",
        _table(
            [
                "total",
                "land disposals",
                "big-box industrial",
                "with point",
                "address/street precision",
                "ambiguous",
                "unresolved/invalid",
                "municipalities",
            ],
            [
                [
                    s.get("total"),
                    s.get("land"),
                    s.get("bigbox"),
                    s.get("resolved"),
                    s.get("precise"),
                    s.get("ambiguous"),
                    s.get("unresolved"),
                    s.get("municipalities"),
                ]
            ],
        ),
        "",
        "Geocode precision: " + ", ".join(f"`{r['k']}` {r['n']}" for r in s.get("by_precision", [])),
        "",
        "## Corpus decisions (crawl ledger)",
        "",
        _table(["decision / reason", "documents"], [[r["k"], r["n"]] for r in data["corpus"]["reasons"]]),
        "",
        "## Validation checks",
        "",
        _table(
            ["check", "severity", "result", "value", "description"],
            [
                [c["id"], c["severity"], "✅ pass" if c["passed"] else "❌ FAIL", c["value"], c["description"]]
                for c in data["checks"]
            ],
        ),
        "",
        "## Quality flags on staged records",
        "",
        _table(["flag", "records"], [[r["k"], r["n"]] for r in s.get("flags", [])]) if s.get("flags") else "_none_",
        "",
        "## Records without a point (documented, not silently dropped)",
        "",
    ]
    no_point = [r for r in data["unresolved"] if r["geocode_status"] != "resolved"]
    if no_point:
        lines.append(
            _table(
                ["id", "status", "location text", "notes", "title"],
                [
                    [
                        r["signal_id"],
                        r["geocode_status"],
                        r["location_text"] or "—",
                        "; ".join(r["geocode_notes"] or [])[:140],
                        r["title"][:90],
                    ]
                    for r in no_point
                ],
            )
        )
    else:
        lines.append("_All staged records carry a validated EPSG:25832 point._")
    lines += ["", "## Issues raised in the reported run", ""]
    issues = [i for i in data["issues"] if i["severity"] in {"error", "warning"}]
    if issues:
        lines.append(
            _table(
                ["severity", "stage", "code", "message"],
                [[i["severity"], i["stage"], i["code"], i["message"][:160]] for i in issues[:60]],
            )
        )
        if len(issues) > 60:
            lines.append(f"\n… {len(issues) - 60} more (see `iris_staging.ingest_issue`).")
    else:
        lines.append("_No errors or warnings._")
    return "\n".join(lines) + "\n"


def signals_csv(rows: list[dict[str, Any]]) -> str:
    fields = [
        "signal_id", "signal_type", "title", "published_at", "deal_type", "area_sqm", "plot_area_sqm",
        "municipality", "district", "street", "postal_code", "geocode_status", "geocode_precision",
        "x_25832", "y_25832", "lon", "lat", "classification_confidence", "quality_flags", "source_url",
    ]  # fmt: skip
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        row = dict(r)
        row["quality_flags"] = ";".join(r.get("quality_flags") or [])
        row["published_at"] = r["published_at"].isoformat() if r.get("published_at") else ""
        writer.writerow(row)
    return buf.getvalue()


def signals_jsonl(rows: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(dict(r), ensure_ascii=False, default=str) + "\n" for r in rows)
