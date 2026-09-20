"""scripts/update_constituents.py: parsing, dry-run default, sanity refusal."""

from __future__ import annotations

import importlib.util
from datetime import date

import pytest

from src.config import REPO_ROOT
from src.data.universe import load_constituents


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location(
        "update_constituents", REPO_ROOT / "scripts" / "update_constituents.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def source_csv(n: int = 500, extra: str = "") -> str:
    lines = ["Symbol,Security,GICS Sector"] + [f"T{i:03d},Company {i},Tech" for i in range(n)]
    return "\n".join(lines) + "\n" + extra


def test_normalizes_share_class_separators(script):
    rows = dict(script.parse_source("Symbol,Security\nBRK-B,Berkshire\nbf.b,Brown-Forman\nAAPL,Apple\n"))
    assert set(rows) == {"BRK.B", "BF.B", "AAPL"}


def test_accepts_alternative_column_names(script):
    assert script.parse_source("Ticker,Name\nAAPL,Apple\n") == [("AAPL", "Apple")]


def test_unrecognised_columns_fail(script):
    with pytest.raises(SystemExit):
        script.parse_source("foo,bar\n1,2\n")


def test_dry_run_writes_nothing(script, tmp_path, capsys):
    src, out = tmp_path / "src.csv", tmp_path / "out.csv"
    src.write_text(source_csv(), encoding="utf-8")
    assert script.main(["--source", str(src), "--out", str(out)]) == 0
    assert not out.exists()
    assert "Dry run" in capsys.readouterr().out


def test_write_produces_a_file_the_loader_reads(script, refresh_cfg):
    src = refresh_cfg.constituents_path.parent / "src.csv"
    src.write_text(source_csv(extra="BRK-B,Berkshire\n"), encoding="utf-8")
    assert script.main(["--source", str(src), "--out", str(refresh_cfg.constituents_path),
                        "--as-of", date.today().isoformat(), "--write"]) == 0
    rows = load_constituents(refresh_cfg)
    assert len(rows) == 501
    assert "BRK B" in {c.symbol for c in rows}         # dot form on disk, IBKR form loaded
    assert refresh_cfg.constituents_path.read_text().splitlines()[0] == "symbol,name,as_of_date"


def test_diff_reports_added_and_removed(script, tmp_path, capsys):
    out = tmp_path / "out.csv"
    script.write_csv(out, [("AAA", "a"), ("OLD", "o")], date.today())
    src = tmp_path / "src.csv"
    src.write_text(source_csv(extra="NEW,New Co\n"), encoding="utf-8")
    script.main(["--source", str(src), "--out", str(out)])
    text = capsys.readouterr().out
    assert "NEW" in text and "OLD" in text.split("removed")[1]


@pytest.mark.parametrize("n", [10, 900])
def test_implausible_list_size_is_refused(script, tmp_path, n):
    src, out = tmp_path / "src.csv", tmp_path / "out.csv"
    src.write_text(source_csv(n), encoding="utf-8")
    assert script.main(["--source", str(src), "--out", str(out), "--write"]) == 1
    assert not out.exists()
