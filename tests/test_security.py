from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

import weread_export_fixed_v13 as app


def test_cookie_selection_uses_exact_domains_and_required_names_only() -> None:
    cookies = [
        {"name": "wr_vid", "value": "parent-vid", "domain": ".qq.com"},
        {"name": "wr_vid", "value": "specific-vid", "domain": ".weread.qq.com"},
        {"name": "wr_skey", "value": "specific-key", "domain": "weread.qq.com"},
        {"name": "wr_name", "value": "profile", "domain": ".weread.qq.com"},
        {"name": "wr_skey", "value": "evil-key", "domain": "weread.qq.com.evil.example"},
        {"name": "wr_vid", "value": "lookalike", "domain": "evilqq.com"},
        {"name": "session", "value": "unrelated", "domain": ".qq.com"},
    ]

    assert app.cookie_list_to_header(cookies) == "wr_vid=specific-vid; wr_skey=specific-key"
    assert app.has_login_cookie(cookies)
    assert app.cookie_metadata(cookies) == [
        {"name": "wr_vid", "domain": ".qq.com"},
        {"name": "wr_vid", "domain": ".weread.qq.com"},
        {"name": "wr_skey", "domain": "weread.qq.com"},
    ]


def test_manual_cookie_header_is_reduced_before_use() -> None:
    raw = "wr_vid=vid; wr_skey=key; wr_name=private-profile; session=unrelated"
    assert app.sanitize_cookie_header(raw) == "wr_vid=vid; wr_skey=key"
    session = app.make_session(raw)
    assert session.cookies.get_dict() == {"wr_vid": "vid", "wr_skey": "key"}


def test_cover_url_requires_https_and_trusted_host() -> None:
    assert app.normalize_cover_url("https://cdn.weread.qq.com/cover.jpg")
    assert app.normalize_cover_url("//cdn.weread.qq.com/cover.jpg").startswith("https://")
    assert app.normalize_cover_url("http://cdn.weread.qq.com/cover.jpg") == ""
    assert app.normalize_cover_url("https://evil.example/cover.jpg") == ""
    assert app.normalize_cover_url("https://weread.qq.com.evil.example/cover.jpg") == ""


def test_cookie_files_are_private_and_raw_metadata_has_no_values(tmp_path: Path) -> None:
    cookie_file = tmp_path / "cookie.txt"
    raw_file = tmp_path / "cookies.json"
    app.save_cookie_files(
        "wr_vid=secret; wr_skey=private",
        [{"name": "wr_vid", "value": "secret", "domain": ".weread.qq.com"}],
        cookie_file,
        raw_file,
    )
    assert stat.S_IMODE(cookie_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(raw_file.stat().st_mode) == 0o600
    assert "secret" not in raw_file.read_text(encoding="utf-8")
    assert json.loads(raw_file.read_text(encoding="utf-8"))[0]["name"] == "wr_vid"


def test_cookie_command_line_argument_is_rejected_without_echo(tmp_path: Path) -> None:
    args = app.build_parser().parse_args(["--cookie", "wr_vid=top-secret"])
    with pytest.raises(RuntimeError) as caught:
        app.get_cookie_string(args, tmp_path)
    assert "top-secret" not in str(caught.value)


def test_generated_html_has_no_inline_error_handler() -> None:
    source = Path(app.__file__).read_text(encoding="utf-8")
    assert ' onerror="' not in source
