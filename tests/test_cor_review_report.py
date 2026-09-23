"""Render-contract tests for the Micron COR Review Agent report."""

from pathlib import Path

import pytest

from datagrid_agents.cli import main
from datagrid_agents.registry import load_definition
from datagrid_agents.reports import (
    CLOSING_LINE,
    SAMPLE_PATH,
    TEMPLATE_PATH,
    load_data_island,
    reconcile_data,
    style_block,
    validate_report,
)

SAMPLE_HTML = SAMPLE_PATH.read_text(encoding="utf-8")
TEMPLATE_HTML = TEMPLATE_PATH.read_text(encoding="utf-8")


def test_sample_report_passes_the_render_contract():
    assert validate_report(SAMPLE_HTML) == []


def test_template_passes_with_placeholders_allowed():
    assert validate_report(TEMPLATE_HTML, allow_placeholders=True) == []


def test_template_and_sample_share_one_branding_block():
    assert style_block(TEMPLATE_HTML) == style_block(SAMPLE_HTML)
    for token in ("--pc-orange:#ff5200", "--pc-black:#0a0a0b", "chip-fail", "@media print"):
        assert token in style_block(SAMPLE_HTML)


def test_sample_arithmetic_reconciles():
    assert reconcile_data(load_data_island(SAMPLE_HTML)) == []


def test_sample_grades_all_three_pillars():
    pillars = load_data_island(SAMPLE_HTML)["pillars"]
    assert set(pillars) == {"cost", "schedule", "technical"}
    assert all(pillars[name]["status"] and pillars[name]["note"] for name in pillars)


def test_every_failing_line_and_gap_has_a_finding():
    data = load_data_island(SAMPLE_HTML)
    failing = {
        line["n"]
        for line in data["line_items"]
        if "Partial" in (line["substantiated"], line["validated"])
        or "Not validated" in (line["substantiated"], line["validated"])
    }
    findings = data["findings"]
    assert failing, "sample should exercise failing lines"
    assert len(findings) >= len(failing)
    assert len(data["document_gaps"]) == 4
    assert all(gap["status"] == "Missing" for gap in data["document_gaps"])
    assert all(finding["pillar"] in {"cost", "schedule", "technical"} for finding in findings)


def test_claimed_schedule_impact_is_flagged_unsubstantiated():
    data = load_data_island(SAMPLE_HTML)
    assert data["schedule_claim"] == {
        "claimed": True,
        "days": 55,
        "amount": 174900.00,
        "substantiated": False,
    }
    assert data["pillars"]["schedule"]["status"] == "Not validated"


def test_rom_is_reference_only_and_not_a_finding():
    data = load_data_island(SAMPLE_HTML)
    rom = data["change_event_rom"]
    assert rom["basis"] == "reference only"
    assert abs(rom["delta_pct"]) <= 10
    assert not any("ROM" in finding["summary"] for finding in data["findings"])


def test_report_stays_self_contained_and_links_to_procore():
    assert "app.datagrid.com" in SAMPLE_HTML
    assert "us02.procore.com" in SAMPLE_HTML
    assert SAMPLE_HTML.count("<script") == 1
    assert "<img" not in SAMPLE_HTML
    assert CLOSING_LINE in SAMPLE_HTML


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda h: h.replace('<td class="rec">None - line closed.</td>', '<td class="rec"></td>'), "empty cell"),
        (lambda h: h.replace(">Not validated<", ">Rejected<"), "outside the allowed vocabulary"),
        (lambda h: h.replace("https://us02.procore.com", "https://example.com"), "not allowed"),
        (lambda h: h.replace('"cor_total": 1124653.75', '"cor_total": 999999.99'), "cor_total"),
        (lambda h: h.replace(CLOSING_LINE, "Thanks for reading!"), "last visible line"),
        (lambda h: h.replace('<section id="cost-breakdown">', '<section id="costs">'), "#cost-breakdown"),
    ],
)
def test_validator_catches_contract_violations(mutate, expected):
    issues = validate_report(mutate(SAMPLE_HTML))
    assert any(expected in issue for issue in issues), issues


def test_definition_loads_prompt_files_and_inlines_the_template():
    definition = load_definition("cor_review_agent")
    assert definition.name == "Micron COR Review Agent"
    assert "THE THREE REVIEW PILLARS" in definition.system_prompt
    assert "HARVEST LINKS WHILE YOU RETRIEVE" in definition.planning_prompt
    assert "{{include:" not in definition.custom_prompt
    assert "<!doctype html>" in definition.custom_prompt
    assert style_block(TEMPLATE_HTML) in definition.custom_prompt
    assert "pdf_page_info" in definition.tools and "calculate" in definition.tools


def test_report_cli_emits_and_validates(tmp_path: Path, capsys):
    out = tmp_path / "report.html"
    assert main(["report", "sample", "--out", str(out)]) == 0
    assert main(["report", "validate", str(out)]) == 0
    assert "passes the COR review render contract" in capsys.readouterr().out

    broken = tmp_path / "broken.html"
    broken.write_text(SAMPLE_HTML.replace(CLOSING_LINE, "see you"), encoding="utf-8")
    assert main(["report", "validate", str(broken)]) == 1
    assert "last visible line" in capsys.readouterr().out

    assert main(["report", "template", "--out", str(tmp_path / "tpl.html")]) == 0
    assert main(["report", "validate", str(tmp_path / "tpl.html"), "--allow-placeholders"]) == 0
