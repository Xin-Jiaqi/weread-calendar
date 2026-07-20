from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import weread_export_fixed_v13 as app


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "weread_export_fixed_v13.py"
FIXTURE = ROOT / "tests" / "fixtures" / "synthetic_daily_reading.csv"


def run_offline(csv_path: Path, out_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--from-csv",
            str(csv_path),
            "--out-dir",
            str(out_dir),
            "--export-png",
            "none",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_synthetic_csv_to_html_and_complete_manifest(tmp_path: Path) -> None:
    out_dir = tmp_path / "report"
    result = run_offline(FIXTURE, out_dir)

    assert result.returncode == 0, result.stderr
    report = out_dir / "reading_report.html"
    manifest_path = out_dir / "weread_run_manifest.json"
    assert report.is_file()
    assert manifest_path.is_file()
    assert "合成示例甲" in report.read_text(encoding="utf-8")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == app.RUN_MANIFEST_SCHEMA_VERSION
    assert manifest["status"] == "complete"
    assert manifest["mode"] == "offline_csv"
    assert manifest["input"] == {
        "kind": "daily_csv",
        "name": FIXTURE.name,
        "sha256": app.file_sha256(FIXTURE),
    }
    assert manifest["counts"] == {"input_rows": 2, "report_rows": 2, "failed_items": 0}
    assert manifest["failed_items"] == []
    assert manifest["outputs"] == [{"kind": "html_report", "path": "reading_report.html"}]
    assert str(FIXTURE.resolve()) not in manifest_path.read_text(encoding="utf-8")


def test_invalid_csv_returns_failure_and_writes_failure_manifest(tmp_path: Path) -> None:
    invalid_csv = tmp_path / "invalid.csv"
    invalid_csv.write_text("日期,总分钟\nnot-a-date,10\n", encoding="utf-8")
    out_dir = tmp_path / "failed-report"

    result = run_offline(invalid_csv, out_dir)

    assert result.returncode != 0
    manifest = json.loads((out_dir / "weread_run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failure"
    assert manifest["mode"] == "offline_csv"
    assert manifest["input"] == {"kind": "daily_csv", "name": "invalid.csv"}
    assert manifest["failed_items"] == [
        {"item_id": "run", "stage": "run", "error_type": "RuntimeError"}
    ]
    assert str(tmp_path) not in json.dumps(manifest, ensure_ascii=False)


def test_completion_status_covers_all_manifest_states() -> None:
    assert app.completion_status(requested=3, failed=0) == "complete"
    assert app.completion_status(requested=3, failed=1) == "partial"
    assert app.completion_status(requested=3, failed=3) == "failure"


def test_failed_item_manifest_excludes_titles_authors_and_raw_errors() -> None:
    items = app.manifest_failed_items([{
        "bookId": "synthetic-004",
        "title": "不应进入 manifest 的标题",
        "author": "不应进入 manifest 的作者",
        "error": "HTTP 503 contained private response text",
    }])

    assert items == [{"item_id": "synthetic-004", "error_code": "http_error"}]
    serialized = json.dumps(items, ensure_ascii=False)
    assert "标题" not in serialized
    assert "作者" not in serialized
    assert "private response" not in serialized
