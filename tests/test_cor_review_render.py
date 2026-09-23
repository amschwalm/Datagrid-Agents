"""Tests for rendering a COR review report from an agent payload."""

import copy
import json
from pathlib import Path

import pytest

from datagrid_agents.cli import main
from datagrid_agents.reports import (
    PAYLOAD_PATH,
    PayloadError,
    SCHEMA,
    build_data_island,
    load_data_island,
    reconcile_data,
    render_report,
    status_classes,
    style_block,
    validate_report,
)
from datagrid_agents.reports.render import rich
from datagrid_agents.reports.validator import TEMPLATE_PATH

PAYLOAD = json.loads(PAYLOAD_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def payload():
    return copy.deepcopy(PAYLOAD)


def test_rendered_payload_passes_the_render_contract():
    assert validate_report(render_report(copy.deepcopy(PAYLOAD))) == []


def test_render_reuses_the_shipped_branding_block():
    html = render_report(copy.deepcopy(PAYLOAD))
    assert style_block(html) == style_block(TEMPLATE_PATH.read_text(encoding="utf-8"))
    assert "{{" not in html
    assert "SKELETON" not in html


def test_payload_is_far_smaller_than_the_document_it_produces():
    # The whole point of the payload: the agent writes this, not 43k of HTML.
    rendered = render_report(copy.deepcopy(PAYLOAD))
    assert len(rendered) > 2 * len(PAYLOAD_PATH.read_text(encoding="utf-8"))


def test_totals_are_derived_not_trusted(payload):
    payload["cost_breakdown"]["rows"][0]["cost"] = 20000.0
    data = load_data_island(render_report(payload))
    assert data["cor_total"] == pytest.approx(20000.0 + 275 + 4015 + 16570 + 2690.7 + 828.5)
    assert reconcile_data(data) == []


def test_data_island_is_derived_from_the_same_numbers():
    data = build_data_island(
        copy.deepcopy(PAYLOAD),
        {"total": 100.0, "direct": 80.0, "covered": 60.0, "coverage_pct": 75.0, "hold": 10.0},
    )
    assert data["schema"] == SCHEMA
    assert (data["cor_total"], data["direct_cost_subtotal"], data["backup_covered"]) == (
        100.0,
        80.0,
        60.0,
    )
    assert data["guardrail"].startswith("Analysis only")


def test_document_gaps_and_pillars_carry_through():
    data = load_data_island(render_report(copy.deepcopy(PAYLOAD)))
    assert [gap["status"] for gap in data["document_gaps"]] == ["Missing", "Missing"]
    assert set(data["pillars"]) == {"cost", "schedule", "technical"}
    assert data["pillars"]["schedule"]["status"] == "Not claimed"


def test_unclaimed_schedule_drops_the_deck_sentence_but_keeps_the_pillar():
    html = render_report(copy.deepcopy(PAYLOAD))
    assert "<strong>Schedule claim:</strong>" not in html
    assert 'data-pillar="schedule"' in html
    assert "not an objection" in html


def test_claimed_schedule_renders_the_deck_sentence(payload):
    payload["verdict"]["schedule"] = "30 days claimed, no TIA located - not substantiated."
    html = render_report(payload)
    assert "<strong>Schedule claim:</strong>" in html
    assert validate_report(html) == []


@pytest.mark.parametrize(
    "token, expected",
    [
        ("Validated", ("chip-ok", "validated")),
        ("Partial - $10 of $20", ("chip-warn", "partial")),
        ("Not validated", ("chip-fail", "not-validated")),
        ("Not found in Procore", ("chip-none", "not-found")),
        ("Missing", ("chip-none", "not-found")),
        ("Present - pkg p.4", ("chip-ok", "validated")),
        ("Present - superseded", ("chip-na", "validated")),
        ("N/A - derived", ("chip-na", "validated")),
    ],
)
def test_status_vocabulary_maps_to_branding(token, expected):
    assert status_classes(token) == expected


def test_off_vocabulary_status_is_rejected(payload):
    payload["cost_breakdown"]["rows"][0]["validated"] = {"status": "Verified", "why": "looks fine"}
    with pytest.raises(PayloadError, match="outside the vocabulary"):
        render_report(payload)


def test_empty_cell_is_rejected_rather_than_rendered(payload):
    payload["cost_breakdown"]["rows"][0]["recommendation"] = ""
    with pytest.raises(PayloadError, match="N/A"):
        render_report(payload)


def test_missing_pillar_is_rejected(payload):
    del payload["pillars"]["schedule"]
    with pytest.raises(PayloadError, match="pillars.schedule"):
        render_report(payload)


def test_backup_cannot_exceed_direct_cost(payload):
    payload["cost_breakdown"]["rows"][0]["backup_covered"] = 999999.0
    with pytest.raises(PayloadError, match="exceeds the direct-cost subtotal"):
        render_report(payload)


def test_links_become_anchors_and_text_is_escaped():
    out = rich("see [CE #391](https://app.procore.com/x) and <script>alert(1)</script>")
    assert '<a class="plink" href="https://app.procore.com/x"' in out
    assert "&lt;script&gt;" in out and "<script>" not in out


def test_datagrid_links_get_the_file_link_class():
    assert 'class="dlink"' in rich("[pkg p.2](https://app.datagrid.com/files?fileId=x)")


def test_forbidden_typography_is_normalised():
    assert "\u2014" not in rich("Stair 3 \u2014 alternate")
    assert "\u201c" not in rich("\u201cRevising with subs\u201d")


def test_render_cli_round_trips(tmp_path: Path, capsys):
    out = tmp_path / "report.html"
    assert main(["report", "render", "--out", str(out)]) == 0
    assert main(["report", "validate", str(out)]) == 0
    assert "passes the COR review render contract" in capsys.readouterr().out

    payload_out = tmp_path / "payload.json"
    assert main(["report", "payload", "--out", str(payload_out)]) == 0
    assert json.loads(payload_out.read_text(encoding="utf-8"))["schema"] == SCHEMA
    assert main(["report", "render", str(payload_out), "--out", str(out)]) == 0


def test_render_cli_reports_a_bad_payload(tmp_path: Path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert main(["report", "render", str(bad)]) == 2

    broken = copy.deepcopy(PAYLOAD)
    broken["pillars"]["cost"]["status"] = "Looks good"
    path = tmp_path / "broken.json"
    path.write_text(json.dumps(broken), encoding="utf-8")
    assert main(["report", "render", str(path)]) == 1
    assert "outside the vocabulary" in capsys.readouterr().err
