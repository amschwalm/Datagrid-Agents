"""Checks for COR review reports emitted by the Micron COR Review Agent.

The agent renders a single self-contained HTML document. These checks encode the
parts of the render contract that can be verified mechanically: required sections,
the status vocabulary, no empty table cells, links limited to Procore/Datagrid,
a parseable JSON data island, and arithmetic that reconciles.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
SAMPLES_DIR = Path(__file__).resolve().parent / "samples"
TEMPLATE_PATH = TEMPLATES_DIR / "cor_review_report.html"
SAMPLE_PATH = SAMPLES_DIR / "cor_review_report_sample.html"
PAYLOAD_PATH = SAMPLES_DIR / "cor_review_payload_harken.json"

CLOSING_LINE = (
    "First-pass analysis only - Micron decides. "
    "Deep-dive detailed report available on request."
)
DATA_ISLAND_ID = "cor-review-data"

REQUIRED_SECTION_IDS = ("verdict", "review-summary", "document-inventory", "cost-breakdown")
OPTIONAL_SECTION_IDS = ("findings", "next-steps", "references")

STATUS_TOKENS = frozenset(
    {
        "Validated",
        "Partial",
        "Not validated",
        "Not found in Procore",
        "Missing",
        "Present",
        "Not claimed",
        "Claimed - substantiated",
        "Claimed - unsubstantiated",
    }
)
STATUS_PREFIXES = ("Present - ", "N/A - ", "Partial - ", "Not found in Procore - ")
STATUS_SLUGS = frozenset({"validated", "partial", "not-validated", "not-found", "open"})
ALLOWED_LINK_HOSTS = ("procore.com", "app.datagrid.com")
REQUIRED_JSON_KEYS = (
    "schema",
    "co_number",
    "verdict",
    "cor_total",
    "direct_cost_subtotal",
    "backup_covered",
    "pillars",
    "change_event_rom",
    "schedule_claim",
    "line_items",
    "guardrail",
)
PILLARS = ("cost", "schedule", "technical")

_PLACEHOLDER_RE = re.compile(r"\{\{[A-Z0-9_]+\}\}")
_PLACEHOLDER_ONLY_RE = re.compile(r"\s*\{\{[A-Z0-9_]+\}\}\s*")
_CENTS = 0.01


@dataclass
class Row:
    """One parsed table row."""

    table: int
    cells: list[str]
    status: str | None


@dataclass
class ParsedReport:
    """The parts of a rendered report these checks care about."""

    rows: list[Row] = field(default_factory=list)
    chips: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    section_ids: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    scripts: list[tuple[dict[str, str], str]] = field(default_factory=list)
    stylesheets: int = 0
    images: int = 0
    text_chunks: list[str] = field(default_factory=list)


class _ReportParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.report = ParsedReport()
        self._cell: list[str] | None = None
        self._cell_depth = 0
        self._row: list[str] | None = None
        self._row_status: str | None = None
        self._table_index = -1
        self._chip: list[str] | None = None
        self._script: dict[str, str] | None = None
        self._script_data: list[str] = []
        self._skip_text = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k: (v or "") for k, v in attrs}
        rep = self.report

        if "data-status" in attr:
            rep.statuses.append(attr["data-status"])
        if tag == "section" and attr.get("id"):
            rep.section_ids.append(attr["id"])
        if tag == "a" and attr.get("href"):
            rep.links.append(attr["href"])
        if tag == "link" and "stylesheet" in attr.get("rel", ""):
            rep.stylesheets += 1
        if tag == "img":
            rep.images += 1
        if tag == "table":
            self._table_index += 1
        if tag == "tr":
            self._row = []
            self._row_status = attr.get("data-status")
        if tag in ("td", "th"):
            if self._cell is None:
                self._cell = []
                self._cell_depth = 1
            else:
                self._cell_depth += 1
        if tag == "span" and "chip" in attr.get("class", "").split():
            self._chip = []
        if tag in ("script", "style"):
            self._skip_text += 1
            if tag == "script":
                self._script = attr
                self._script_data = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None:
            self._cell_depth -= 1
            if self._cell_depth == 0:
                text = " ".join(" ".join(self._cell).split())
                if self._row is not None:
                    self._row.append(text)
                self._cell = None
        if tag == "tr" and self._row is not None:
            self.report.rows.append(
                Row(table=self._table_index, cells=self._row, status=self._row_status)
            )
            self._row = None
            self._row_status = None
        if tag == "span" and self._chip is not None:
            self.report.chips.append(" ".join("".join(self._chip).split()))
            self._chip = None
        if tag in ("script", "style"):
            self._skip_text = max(0, self._skip_text - 1)
            if tag == "script" and self._script is not None:
                self.report.scripts.append((self._script, "".join(self._script_data)))
                self._script = None

    def handle_data(self, data: str) -> None:
        if self._script is not None:
            self._script_data.append(data)
        if self._skip_text:
            return
        if self._chip is not None:
            self._chip.append(data)
        if self._cell is not None:
            self._cell.append(data)
        stripped = " ".join(data.split())
        if stripped:
            self.report.text_chunks.append(stripped)


def parse_report(html: str) -> ParsedReport:
    parser = _ReportParser()
    parser.feed(html)
    parser.close()
    return parser.report


def _status_ok(token: str) -> bool:
    return token in STATUS_TOKENS or token.startswith(STATUS_PREFIXES)


def _is_placeholder(value: str) -> bool:
    return bool(_PLACEHOLDER_ONLY_RE.fullmatch(value))


def _check_shell(html: str, issues: list[str]) -> None:
    head = html.lstrip()[:120].lower()
    if not head.startswith("<!doctype html>"):
        issues.append("document must start with <!doctype html>")
    if not html.rstrip().endswith("</html>"):
        issues.append("document must end with </html>")
    if 'id="cor-review-data"' not in html:
        issues.append("missing JSON data island (script#cor-review-data)")
    if "@import" in html:
        issues.append("no @import allowed: the report must be self-contained")
    for pattern in ('src="http', "src='http", 'href="http://'):
        if pattern in html and "app.datagrid.com" not in pattern:
            if pattern.startswith("src"):
                issues.append("no external assets allowed: found remote src reference")


def _check_structure(report: ParsedReport, issues: list[str]) -> None:
    for section_id in REQUIRED_SECTION_IDS:
        if section_id not in report.section_ids:
            issues.append(f"missing required section: #{section_id}")
    unknown = set(report.section_ids) - set(REQUIRED_SECTION_IDS) - set(OPTIONAL_SECTION_IDS)
    for section_id in sorted(unknown):
        if not re.fullmatch(r"(deep-dive|addendum)[a-z0-9-]*", section_id):
            issues.append(f"unexpected section id: #{section_id}")
    if report.stylesheets:
        issues.append("external stylesheet links are not allowed")
    if report.images:
        issues.append("<img> is not allowed; the inline SVG mark is the only graphic")

    data_scripts = [s for s in report.scripts if s[0].get("id") == DATA_ISLAND_ID]
    if len(data_scripts) != 1:
        issues.append("exactly one script#cor-review-data is required")
    for attrs, _ in report.scripts:
        if attrs.get("id") != DATA_ISLAND_ID:
            issues.append("no JavaScript allowed besides the JSON data island")
        elif attrs.get("type") != "application/json":
            issues.append("data island must be type=application/json")

    if not report.text_chunks:
        issues.append("document has no visible text")
    elif report.text_chunks[-1] != CLOSING_LINE:
        issues.append(f"last visible line must be exactly: {CLOSING_LINE!r}")


def _check_rows(report: ParsedReport, issues: list[str], skip_placeholders: bool) -> None:
    for row in report.rows:
        if not row.cells:
            continue
        for index, cell in enumerate(row.cells):
            if not cell:
                label = row.cells[1] if len(row.cells) > 1 else f"table {row.table}"
                issues.append(f"empty cell {index} in row {label!r} - write 'N/A - <why>' instead")
        if skip_placeholders and row.status and _is_placeholder(row.status):
            continue
        if row.status and row.status not in STATUS_SLUGS:
            issues.append(f"unknown data-status {row.status!r} (allowed: {sorted(STATUS_SLUGS)})")
    widths = {}
    for row in report.rows:
        widths.setdefault(row.table, set()).add(len(row.cells))
    for table, seen in widths.items():
        if len(seen) > 1:
            issues.append(f"table {table} has ragged rows: cell counts {sorted(seen)}")


def _check_tokens(report: ParsedReport, issues: list[str], skip_placeholders: bool) -> None:
    for chip in report.chips:
        if skip_placeholders and _is_placeholder(chip):
            continue
        if not _status_ok(chip):
            issues.append(f"status token {chip!r} is outside the allowed vocabulary")
    for status in report.statuses:
        if skip_placeholders and _is_placeholder(status):
            continue
        if status not in STATUS_SLUGS:
            issues.append(f"unknown data-status value {status!r}")


def _check_links(report: ParsedReport, issues: list[str], skip_placeholders: bool) -> None:
    for href in report.links:
        if href.startswith("#"):
            continue
        if skip_placeholders and _is_placeholder(href):
            continue
        if not href.startswith("https://"):
            issues.append(f"link must be https and absolute: {href!r}")
            continue
        host = href.split("/", 3)[2].lower()
        if not any(host == allowed or host.endswith("." + allowed) for allowed in ALLOWED_LINK_HOSTS):
            issues.append(f"link host not allowed (Procore/Datagrid only): {href!r}")


def load_data_island(html: str) -> dict[str, Any]:
    """Return the parsed JSON data island from a rendered report."""
    for attrs, data in parse_report(html).scripts:
        if attrs.get("id") == DATA_ISLAND_ID:
            return json.loads(data)
    raise ValueError("report has no script#cor-review-data data island")


def reconcile_data(data: dict[str, Any]) -> list[str]:
    """Check the data island's arithmetic against its own line items."""
    issues: list[str] = []
    lines = data.get("line_items") or []
    if not lines:
        return ["data island has no line_items"]

    total = round(sum(float(line["cost"]) for line in lines), 2)
    claimed_total = round(float(data.get("cor_total", 0)), 2)
    if abs(total - claimed_total) > _CENTS:
        issues.append(f"line items sum to {total:,.2f} but cor_total is {claimed_total:,.2f}")

    direct = round(sum(float(x["cost"]) for x in lines if not x.get("derived")), 2)
    claimed_direct = round(float(data.get("direct_cost_subtotal", 0)), 2)
    if abs(direct - claimed_direct) > _CENTS:
        issues.append(
            f"non-derived lines sum to {direct:,.2f} but direct_cost_subtotal is {claimed_direct:,.2f}"
        )

    covered = round(
        sum(float(x.get("backup_covered", 0)) for x in lines if not x.get("derived")), 2
    )
    claimed_covered = round(float(data.get("backup_covered", 0)), 2)
    if abs(covered - claimed_covered) > _CENTS:
        issues.append(
            f"per-line backup covers {covered:,.2f} but backup_covered is {claimed_covered:,.2f}"
        )
    if claimed_covered > claimed_direct + _CENTS:
        issues.append("backup_covered cannot exceed direct_cost_subtotal")

    position = data.get("recommended_position")
    adjustment = data.get("recommended_adjustment")
    if position is not None and adjustment is not None:
        expected = round(claimed_total - float(position), 2)
        if abs(expected - round(float(adjustment), 2)) > _CENTS:
            issues.append(
                f"recommended_adjustment should be {expected:,.2f} "
                f"(cor_total - recommended_position)"
            )

    rom = data.get("change_event_rom") or {}
    if rom.get("amount"):
        delta = round(claimed_total - float(rom["amount"]), 2)
        if abs(delta - round(float(rom.get("delta_vs_cor", 0)), 2)) > _CENTS:
            issues.append(f"change_event_rom.delta_vs_cor should be {delta:,.2f}")

    for line in lines:
        for key in ("n", "description", "cost", "substantiated", "validated"):
            if key not in line:
                issues.append(f"line item {line.get('n', '?')} missing '{key}'")
        if line.get("pillar") and line["pillar"] not in PILLARS:
            issues.append(f"line item {line.get('n', '?')} has unknown pillar {line['pillar']!r}")
    return issues


def _check_data_island(html: str, issues: list[str]) -> None:
    try:
        data = load_data_island(html)
    except ValueError:
        return
    except json.JSONDecodeError as exc:
        issues.append(f"data island is not valid JSON: {exc}")
        return

    for key in REQUIRED_JSON_KEYS:
        if key not in data:
            issues.append(f"data island missing key: {key}")
    pillars = data.get("pillars") or {}
    for pillar in PILLARS:
        entry = pillars.get(pillar)
        if not isinstance(entry, dict) or not entry.get("status"):
            issues.append(f"data island pillar '{pillar}' needs a status")
        elif not _status_ok(str(entry["status"])):
            issues.append(f"pillar '{pillar}' status {entry['status']!r} is outside the vocabulary")
    issues.extend(reconcile_data(data))


def validate_report(html: str, *, allow_placeholders: bool = False) -> list[str]:
    """Return a list of render-contract violations; empty means the report passes."""
    issues: list[str] = []
    _check_shell(html, issues)
    if not allow_placeholders:
        placeholders = sorted(set(_PLACEHOLDER_RE.findall(html)))
        if placeholders:
            issues.append(f"unfilled placeholders: {', '.join(placeholders[:8])}")

    report = parse_report(html)
    _check_structure(report, issues)
    _check_rows(report, issues, allow_placeholders)
    _check_tokens(report, issues, allow_placeholders)
    _check_links(report, issues, allow_placeholders)
    if not allow_placeholders:
        _check_data_island(html, issues)
    return issues


def validate_file(path: str | Path, *, allow_placeholders: bool = False) -> list[str]:
    html = Path(path).read_text(encoding="utf-8")
    return validate_report(html, allow_placeholders=allow_placeholders)


def style_block(html: str) -> str:
    """Return the <style> block, used to prove template/sample branding parity."""
    start = html.index("<style>")
    end = html.index("</style>") + len("</style>")
    return html[start:end]
