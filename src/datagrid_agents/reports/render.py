"""Render a COR review report from the compact payload an agent can actually emit.

Emitting the whole branded document inline costs an agent ~66k characters, most of
it the fixed head and stylesheet. Measured against the live Datagrid agent, that
render does not return in a usable amount of time, while a compact JSON payload of
the same review comes back in minutes. So the agent produces the analysis as a
payload and this module turns it into the shipped HTML: the head and `<style>` block
are copied verbatim from the template, and every derived figure - subtotals, totals,
backup coverage, exception counts, the JSON data island - is computed here rather
than trusted to the model.
"""

from __future__ import annotations

import html
import json
import re
from typing import Any

from datagrid_agents.reports.validator import CLOSING_LINE, TEMPLATE_PATH

SCHEMA = "micron.cor-review/1.0"
DEFAULT_AGENT_NAME = "Micron COR Review Agent"

VERDICT_TOKENS = (
    "Not valid",
    "Weak justification",
    "Partially justified",
    "Mostly justified",
    "Fully justified",
)
_VERDICT_SLUGS = {
    "Fully justified": "validated",
    "Mostly justified": "validated",
    "Partially justified": "partial",
    "Weak justification": "partial",
    "Not valid": "not-validated",
}

# token prefix -> (chip class, row data-status)
_STATUS_MAP: tuple[tuple[str, str, str], ...] = (
    ("N/A", "chip-na", "validated"),
    ("Present - superseded", "chip-na", "validated"),
    ("Present", "chip-ok", "validated"),
    ("Validated", "chip-ok", "validated"),
    ("Not claimed", "chip-na", "validated"),
    ("Claimed - substantiated", "chip-ok", "validated"),
    ("Claimed - unsubstantiated", "chip-fail", "not-validated"),
    ("Partial", "chip-warn", "partial"),
    ("Not validated", "chip-fail", "not-validated"),
    ("Not found in Procore", "chip-none", "not-found"),
    ("Missing", "chip-none", "not-found"),
    ("Open", "chip-warn", "open"),
)
_EXCEPTION_SLUGS = frozenset({"partial", "not-validated", "not-found"})

_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https://[^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_AMOUNT_RE = re.compile(r"\$\d[\d,]*(?:\.\d{2})?")
_PUNCT = {
    "\u2014": " - ",
    "\u2013": "-",
    "\u2018": "'",
    "\u2019": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u2026": "...",
    "\u00a0": " ",
}


class PayloadError(ValueError):
    """The payload cannot be rendered into a contract-valid report."""


# --------------------------------------------------------------------------- text


def _ascii(text: str) -> str:
    """Normalise the typography the contract forbids (em dashes, smart quotes)."""
    for bad, good in _PUNCT.items():
        text = text.replace(bad, good)
    return re.sub(r" {2,}", " ", text).strip()


def _esc(value: Any) -> str:
    return html.escape(_ascii(str(value)), quote=False)


def _link(label: str, url: str) -> str:
    cls = "dlink" if "app.datagrid.com" in url else "plink"
    return (
        f'<a class="{cls}" href="{html.escape(url, quote=True)}" '
        f'target="_blank" rel="noopener">{_esc(label)}</a>'
    )


def rich(value: Any) -> str:
    """Escape text, then re-enable `[label](https://...)` links and `**bold**`.

    Agents write links reliably in this shorthand and unreliably in raw anchors, so
    the payload carries the shorthand and the anchor markup is generated here.
    """
    if value is None:
        return ""
    text = _ascii(str(value))
    placeholders: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        placeholders.append(_link(match.group(1), match.group(2)))
        return f"\x00{len(placeholders) - 1}\x00"

    text = _LINK_RE.sub(_stash, text)
    text = html.escape(text, quote=False)
    text = _BOLD_RE.sub(r"<strong>\1</strong>", text)
    for index, anchor in enumerate(placeholders):
        text = text.replace(f"\x00{index}\x00", anchor)
    return text


def _amounts(text: str) -> str:
    """Wrap dollar figures so findings render them the way the template styles them."""
    return _AMOUNT_RE.sub(lambda m: f'<span class="amt">{m.group(0)}</span>', text)


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _pct(value: float) -> str:
    return f"{value:.1f}%"


# --------------------------------------------------------------------------- status


def status_classes(token: str) -> tuple[str, str]:
    """Map a status token to its chip class and row `data-status` slug."""
    clean = _ascii(str(token))
    for prefix, chip, slug in _STATUS_MAP:
        if clean == prefix or clean.startswith(prefix + " -") or clean.startswith(prefix + " "):
            return chip, slug
    raise PayloadError(
        f"status token {token!r} is outside the vocabulary "
        "(Validated / Partial / Not validated / Not found in Procore / Present / Missing / N/A - ...)"
    )


def _chip(token: str, *, large: bool = False) -> str:
    chip, _ = status_classes(token)
    size = " chip-lg" if large else ""
    return f'<span class="chip {chip}{size}">{_esc(token)}</span>'


def _graded_cell(entry: Any) -> str:
    """Render a `{status, why}` cell: chip plus the reason, never a bare token."""
    if isinstance(entry, str):
        entry = {"status": entry, "why": ""}
    if not isinstance(entry, dict) or not entry.get("status"):
        raise PayloadError(f"graded cell needs a 'status': {entry!r}")
    why = rich(entry.get("why", ""))
    tail = f'<span class="why">{why}</span>' if why else ""
    return _chip(entry["status"]) + tail


def _require(payload: dict[str, Any], key: str) -> Any:
    if key not in payload:
        raise PayloadError(f"payload is missing required key: {key}")
    return payload[key]


def _cell(value: Any, *, field: str) -> str:
    """No empty cells anywhere: the contract wants 'N/A - <why>' instead."""
    text = rich(value)
    if not text.strip():
        raise PayloadError(f"{field} is empty; write \"N/A - <why>\" instead")
    return text


# --------------------------------------------------------------------------- blocks


def _head(payload: dict[str, Any], totals: dict[str, float]) -> str:
    """Reuse the template's head verbatim so branding cannot drift."""
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    head = template[: template.index("</head>") + len("</head>")]
    head = head[head.index("<!doctype html>") :]
    # The skeleton's own "how to fill this in" comment is not part of a rendered report.
    head = re.sub(r"<!--.*?-->\s*", "", head, count=1, flags=re.S)

    ident = payload["identity"]
    title = (
        f"COR Review - {ident['co_number']} - {ident['subject']} - {ident['contractor']}"
    )
    head = re.sub(r"<title>.*?</title>", f"<title>{_esc(title)}</title>", head, flags=re.S)
    head = head.replace("{{VERDICT_TOKEN}}", _esc(payload["verdict"]["token"]))
    head = head.replace("{{COR_TOTAL_NUMERIC}}", f"{totals['total']:.2f}")
    return head


def _masthead(payload: dict[str, Any], totals: dict[str, float]) -> str:
    ident = payload["identity"]
    agent = _esc(payload.get("agent_name") or DEFAULT_AGENT_NAME)
    generated = _esc(_require(payload, "generated"))

    co_url = ident.get("co_url")
    if co_url:
        action = (
            f'<a class="btn" href="{html.escape(str(co_url), quote=True)}" '
            'target="_blank" rel="noopener">Open CO in Procore</a>'
        )
    else:
        action = '<span class="src">No CO record link - open in Procore Change Orders</span>'

    fields = (
        ("Project", ident.get("project")),
        ("Project no.", ident.get("project_number")),
        ("Owner", ident.get("owner")),
        ("General contractor", ident.get("gc")),
        ("Subcontractor", ident.get("contractor")),
        ("Commitment", ident.get("commitment")),
        ("COR received", ident.get("date_received")),
        ("Package", f"{ident.get('package_pages', 0)} pp. indexed"),
    )
    meta = "\n".join(
        f"        <div><dt>{_esc(label)}</dt>"
        f'<dd class="num">{_cell(value, field=f"identity.{label}")}</dd></div>'
        for label, value in fields
    )

    subtitle = (
        f"{ident['contractor']} - {_money(totals['total'])} as submitted "
        f"- {ident['pricing_basis']}"
    )
    return f"""<header class="masthead">
  <div class="wrap">
    <div class="brandbar">
      <span class="mark">
        <svg width="26" height="26" viewBox="0 0 26 26" role="img" aria-label="Procore">
          <rect width="26" height="26" rx="5" fill="#ff5200"></rect>
          <path d="M8.2 19.6V6.4h5.2c2.9 0 4.7 1.7 4.7 4.4s-1.8 4.5-4.7 4.5h-2.3v4.3zm3-6.8h2c1.3 0 2.1-.8 2.1-2s-.8-2-2.1-2h-2z" fill="#fff"></path>
        </svg>
        <span class="wordmark">Procore</span>
      </span>
      <span class="sep">|</span>
      <span class="src">Connected project data - all findings sourced from Procore</span>
      <span class="right">
        <span>{agent} - {generated}</span>
        {action}
      </span>
    </div>

    <div class="titleblock">
      <p class="eyebrow">Change Order Request Review - First-Pass Analysis</p>
      <h1><span class="co">{_esc(ident['co_number'])}</span> - {_esc(ident['subject'])}</h1>
      <p class="subtitle">{_esc(subtitle)}</p>

      <dl class="meta-grid">
{meta}
      </dl>
    </div>
  </div>
</header>"""


def _deck(payload: dict[str, Any], totals: dict[str, float]) -> str:
    verdict = payload["verdict"]
    token = _ascii(str(verdict["token"]))
    if token not in VERDICT_TOKENS:
        raise PayloadError(f"verdict token {token!r} must be one of {list(VERDICT_TOKENS)}")

    body = [f"<p>{rich(verdict['reason'])}</p>"]
    coverage = (
        f"backup covers {_money(totals['covered'])} of {_money(totals['direct'])} "
        f"in direct cost ({_pct(totals['coverage_pct'])}); "
        f"{_money(totals['direct'] - totals['covered'])} has no located basis."
    )
    body.append(f"<p><strong>Backup coverage:</strong> {_esc(coverage)}</p>")
    if verdict.get("schedule"):
        body.append(f"<p><strong>Schedule claim:</strong> {rich(verdict['schedule'])}</p>")
    vbody = "\n      ".join(body)

    metrics = "\n    ".join(_metric_cards(payload, totals))
    pillars = "\n    ".join(_pillar_cards(payload))
    return f"""<div class="wrap deck">
  <div class="verdict" data-status="{_VERDICT_SLUGS[token]}">
    <div>
      <div class="vlabel">Verdict</div>
      <div class="vtoken">{_esc(token)}</div>
    </div>
    <div class="vbody">
      {vbody}
    </div>
  </div>

  <div class="metrics">
    {metrics}
  </div>

  <div class="pillars">
    {pillars}
  </div>
</div>"""


def _metric_cards(payload: dict[str, Any], totals: dict[str, float]) -> list[str]:
    """Five fixed metrics, all derived from the cost rows and the inventory."""
    notes = payload.get("metric_notes") or {}
    rows = payload["cost_breakdown"]["rows"]
    exceptions = sum(1 for row in rows if _row_slug(row) in _EXCEPTION_SLUGS)
    gaps = sum(
        1
        for row in payload["document_inventory"]["rows"]
        if status_classes(row["status"])[1] == "not-found"
    )
    hold = totals["hold"]
    hold_note = (
        f"{_pct(100 * hold / totals['total'])} of submitted total"
        if hold and totals["total"]
        else "No quantified adjustment"
    )
    cards = (
        ("COR as submitted", _money(totals["total"]), notes.get("total", f"{len(rows)} priced lines"), True),
        ("Backup coverage", _pct(totals["coverage_pct"]),
         f"{_money(totals['covered'])} of {_money(totals['direct'])} direct", False),
        ("Lines w/ exceptions", f"{exceptions} / {len(rows)}",
         notes.get("exceptions", "Graded from the page-inventory coverage map"), False),
        ("Documentation gaps", str(gaps),
         notes.get("gaps", "Missing rows in the document inventory"), False),
        ("Recommended reduction / hold",
         _money(hold) if hold else "N/A", notes.get("hold", hold_note), True),
    )
    return [
        f'<div class="metric{" accent" if accent else ""}"><div class="k">{_esc(k)}</div>'
        f'<div class="v">{_esc(v)}</div><div class="n">{_cell(n, field="metric note")}</div></div>'
        for k, v, n, accent in cards
    ]


def _pillar_cards(payload: dict[str, Any]) -> list[str]:
    labels = (("cost", "01", "Cost review"), ("schedule", "02", "Schedule review"),
              ("technical", "03", "Technical / scope review"))
    cards = []
    for key, num, label in labels:
        pillar = payload["pillars"].get(key)
        if not pillar:
            raise PayloadError(f"pillars.{key} is required; a pillar is never omitted")
        chip, slug = status_classes(pillar["status"])
        points = pillar.get("points") or []
        if not points:
            raise PayloadError(f"pillars.{key} needs at least one point")
        items = "\n        ".join(f"<li>{rich(point)}</li>" for point in points)
        cards.append(
            f'<article class="pillar" data-pillar="{key}" data-status="{slug}">\n'
            f'      <h3><span class="pnum">{num}</span>{label} '
            f'<span class="chip {chip}">{_esc(pillar["status"])}</span></h3>\n'
            f"      <ul>\n        {items}\n      </ul>\n    </article>"
        )
    return cards


def _section(number: int, title: str, section_id: str, note: str, body: str) -> str:
    note_html = f'\n    <p class="sec-note">{rich(note)}</p>' if note else ""
    return f"""  <section id="{section_id}">
    <h2><span class="sec-num">{number}</span>{_esc(title)}</h2>{note_html}
    <div class="rule"></div>
{body}
  </section>"""


def _verdict_section(number: int, payload: dict[str, Any]) -> str:
    verdict = payload["verdict"]
    body = f"    <p>{rich(verdict['paragraph'])}</p>"
    rom = verdict.get("rom")
    if rom:
        body += f'\n    <p class="sec-note" data-role="rom-context">{rich(rom)}</p>'
    return _section(number, "Verdict", "verdict", "", body)


_SUMMARY_ROWS = (
    ("change_type", "Change type"),
    ("pricing_basis", "Pricing basis"),
    ("source_of_change", "Source of change"),
    ("backup", "Backup substantiates total?"),
    ("cost_codes", "Cost codes correct?"),
    ("markups", "Markups &amp; fees per contract?"),
    ("schedule", "Schedule impact"),
    ("rom", "Change Event ROM (reference only)"),
)


def _review_summary(number: int, payload: dict[str, Any]) -> str:
    summary = payload["review_summary"]
    rows = []
    for key, label in _SUMMARY_ROWS:
        entry = summary.get(key)
        if entry is None:
            raise PayloadError(f"review_summary.{key} is required; every row renders every run")
        if isinstance(entry, str):
            entry = {"text": entry}
        chip = _chip(entry["status"]) + " " if entry.get("status") else ""
        attr = ' data-role="rom-context"' if key == "rom" else ""
        value = _cell(entry.get("text"), field=f"review_summary.{key}")
        rows.append(f'          <tr{attr}><th scope="row">{label}</th><td>{chip}{value}</td></tr>')
    body = (
        '    <div class="tw">\n      <table class="kv">\n'
        "        <caption>Fixed rows - every review renders all of them.</caption>\n"
        "        <tbody>\n" + "\n".join(rows) + "\n        </tbody>\n      </table>\n    </div>"
    )
    return _section(number, "Review Summary", "review-summary", "", body)


def _document_inventory(number: int, payload: dict[str, Any]) -> str:
    block = payload["document_inventory"]
    rows = []
    for index, row in enumerate(block["rows"], start=1):
        chip, slug = status_classes(row["status"])
        why = rich(row.get("where", ""))
        status_cell = f'<span class="chip {chip}">{_esc(row["status"])}</span>' + (
            f'<span class="why">{why}</span>' if why else ""
        )
        rows.append(
            f'          <tr data-status="{slug}">\n'
            f'            <td class="idx">{index}</td>\n'
            f'            <td>{_cell(row.get("document"), field="inventory.document")}</td>\n'
            f'            <td>{_cell(row.get("type"), field="inventory.type")}</td>\n'
            f"            <td>{status_cell}</td>\n"
            f'            <td>{_cell(row.get("supports"), field="inventory.supports")}</td>\n'
            f'            <td>{_cell(row.get("cite"), field="inventory.cite")}</td>\n'
            f"          </tr>"
        )
    body = (
        '    <div class="tw">\n      <table class="rpt">\n'
        "        <caption>One row per document the COR cites, includes, or requires "
        "- including the absent ones.</caption>\n"
        "        <thead>\n"
        '          <tr><th scope="col">#</th><th scope="col">Document</th><th scope="col">Type</th>\n'
        '              <th scope="col">Status</th><th scope="col">Supports</th>'
        '<th scope="col">Cite</th></tr>\n'
        "        </thead>\n        <tbody>\n" + "\n".join(rows) + "\n        </tbody>\n"
        "      </table>\n    </div>"
    )
    return _section(
        number, "Substantiation - Document Inventory", "document-inventory",
        block.get("note", ""), body,
    )


def _row_slug(row: dict[str, Any]) -> str:
    """A line's row status is the worse of its Substantiated and Validated grades."""
    order = ["validated", "open", "partial", "not-found", "not-validated"]
    slugs = [status_classes(row[key]["status"] if isinstance(row[key], dict) else row[key])[1]
             for key in ("substantiated", "validated")]
    return max(slugs, key=order.index)


def _cost_totals(payload: dict[str, Any]) -> dict[str, float]:
    rows = payload["cost_breakdown"]["rows"]
    if not rows:
        raise PayloadError("cost_breakdown.rows is empty; every priced line gets a row")
    total = round(sum(float(row["cost"]) for row in rows), 2)
    direct = round(sum(float(row["cost"]) for row in rows if not row.get("derived")), 2)
    covered = round(
        sum(float(row.get("backup_covered", 0) or 0) for row in rows if not row.get("derived")), 2
    )
    if covered > direct + 0.01:
        raise PayloadError("backup_covered across lines exceeds the direct-cost subtotal")
    rollup = payload["cost_breakdown"].get("rollup") or {}
    hold = round(
        sum(abs(float(line["amount"])) for line in rollup.get("lines", [])
            if str(line.get("kind")) == "deduct"),
        2,
    )
    return {
        "total": total,
        "direct": direct,
        "covered": covered,
        "coverage_pct": (100 * covered / direct) if direct else 0.0,
        "hold": hold,
    }


def _cost_breakdown(number: int, payload: dict[str, Any], totals: dict[str, float]) -> str:
    block = payload["cost_breakdown"]
    rows: list[str] = []
    for row in block["rows"]:
        slug = _row_slug(row)
        basis = rich(row.get("basis", ""))
        item = _cell(row.get("item"), field="cost row item")
        if basis:
            item += f'<span class="why">{basis}</span>'
        rows.append(
            f'          <tr data-status="{slug}" data-amount="{float(row["cost"]):.2f}">\n'
            f'            <td class="idx">{_esc(row.get("n", "-"))}</td>\n'
            f"            <td>{item}</td>\n"
            f'            <td class="money">{_money(float(row["cost"]))}</td>\n'
            f"            <td>{_graded_cell(row['substantiated'])}</td>\n"
            f"            <td>{_graded_cell(row['validated'])}</td>\n"
            f'            <td>{_cell(row.get("reference"), field="cost row reference")}</td>\n'
            f'            <td class="rec">{_cell(row.get("recommendation"), field="cost row recommendation")}</td>\n'
            f"          </tr>"
        )

    derived = [row for row in block["rows"] if row.get("derived")]
    if derived:
        rows.insert(
            len(block["rows"]) - len(derived),
            '          <tr class="subtotal" data-status="partial">\n'
            '            <td class="idx">-</td>\n'
            "            <td>Subtotal - direct cost</td>\n"
            f'            <td class="money">{_money(totals["direct"])}</td>\n'
            f"            <td>backup covers {_money(totals['covered'])} of {_money(totals['direct'])} "
            f"({_pct(totals['coverage_pct'])})</td>\n"
            "            <td>See per-line grades above</td>\n"
            "            <td>Sum computed from the line items</td>\n"
            '            <td class="rec">N/A - subtotal; see per-line recommendations.</td>\n'
            "          </tr>",
        )

    total_row = block.get("total") or {}
    rows.append(
        f'          <tr class="total" data-status="{status_classes(total_row.get("status", "Partial"))[1]}" '
        f'data-amount="{totals["total"]:.2f}">\n'
        '            <td class="idx">-</td>\n'
        "            <td>Total - COR as submitted"
        '<span class="why">Sum of every priced line above</span></td>\n'
        f'            <td class="money">{_money(totals["total"])}</td>\n'
        f'            <td>{_chip(total_row.get("status", "Partial"))}'
        f'<span class="why">backup covers {_money(totals["covered"])} of '
        f'{_money(totals["direct"])} direct cost ({_pct(totals["coverage_pct"])})</span></td>\n'
        f'            <td>{_cell(total_row.get("validated", "N/A - see per-line grades"), field="total.validated")}</td>\n'
        f'            <td>{_cell(total_row.get("reference", "N/A - see per-line references"), field="total.reference")}</td>\n'
        f'            <td class="rec">{_cell(total_row.get("recommendation"), field="total.recommendation")}</td>\n'
        "          </tr>"
    )

    body = (
        '    <div class="tw">\n      <table class="rpt">\n'
        "        <caption>Every priced line from the COR, graded from the page-inventory "
        "coverage map. No empty cells.</caption>\n"
        "        <thead>\n"
        '          <tr><th scope="col">#</th><th scope="col">Line item</th>'
        '<th scope="col" class="money">Cost</th>\n'
        '              <th scope="col">Substantiated</th><th scope="col">Validated</th>\n'
        '              <th scope="col">Reference</th><th scope="col">Recommendation</th></tr>\n'
        "        </thead>\n        <tbody>\n" + "\n".join(rows) + "\n        </tbody>\n"
        "      </table>\n    </div>"
    )
    body += _rollup(block.get("rollup"))
    return _section(
        number, "Cost Breakdown & Validation", "cost-breakdown", block.get("note", ""), body
    )


def _rollup(rollup: dict[str, Any] | None) -> str:
    if not rollup or not rollup.get("lines"):
        return ""
    lines = []
    for line in rollup["lines"]:
        amount = float(line["amount"])
        kind = str(line.get("kind", "line"))
        cls = ' class="neg"' if kind == "deduct" else (' class="sum"' if kind == "sum" else "")
        shown = f"({_money(abs(amount))})" if kind == "deduct" else _money(amount)
        lines.append(
            f'          <tr{cls}><td>{rich(line["label"])}</td>'
            f'<td class="money">{shown}</td></tr>'
        )
    note = rich(
        rollup.get("note")
        or "Arithmetic roll-up of the per-line findings - not an approval, not a negotiating "
        "position, and not a substitute for repricing by the subcontractor."
    )
    title = _esc(rollup.get("title") or "Illustrative recommended position")
    return (
        '\n\n    <div class="recon">\n'
        f"      <h3>{title}</h3>\n"
        "      <table>\n        <tbody>\n" + "\n".join(lines) + "\n        </tbody>\n"
        "      </table>\n"
        f'      <p class="foot">{note}</p>\n    </div>'
    )


def _findings(number: int, payload: dict[str, Any]) -> str:
    findings = payload.get("findings") or []
    if not findings:
        return ""
    items = []
    for finding in findings:
        pillar = str(finding.get("pillar", "cost")).lower()
        chip, slug = status_classes(finding["status"])
        badge = "Open" if slug == "open" else pillar.capitalize()
        items.append(
            f'      <li data-status="{slug}" data-pillar="{pillar}">\n'
            f'        <span class="badge" data-pillar="{pillar}">{_esc(badge)}</span>\n'
            f"        {_amounts(rich(finding['text']))}\n      </li>"
        )
    body = '    <ul class="findings">\n' + "\n".join(items) + "\n    </ul>"
    return _section(
        number, "Findings", "findings",
        payload.get("findings_note")
        or "Exceptions only - objections first, then open items with a resolution path. "
        "Validated lines are carried in the Cost Breakdown table and are not repeated here.",
        body,
    )


def _next_steps(number: int, payload: dict[str, Any]) -> str:
    steps = payload.get("next_steps") or []
    if not steps:
        return ""
    items = "\n".join(f"      <li>{rich(step)}</li>" for step in steps[:5])
    return _section(number, "Next Steps", "next-steps", "", f'    <ol class="steps">\n{items}\n    </ol>')


def _references(number: int, payload: dict[str, Any]) -> str:
    refs = payload.get("references") or []
    if not refs:
        return ""
    items = []
    for ref in refs:
        label = _esc(ref["label"])
        url = ref.get("url")
        anchor = _link(ref["label"], url) if url else (
            f'{label} <span class="nolink">(no link - open in Procore '
            f'{_esc(ref.get("tool", "Change Orders"))})</span>'
        )
        items.append(f'      <li><span class="rtype">{_esc(ref["type"])}</span>{anchor}</li>')
    body = '    <ul class="refs">\n' + "\n".join(items) + "\n    </ul>"
    return _section(
        number, "Procore References", "references",
        "Every Procore record relied on in this review. Links open the source record "
        "in the connected Procore project.",
        body,
    )


def _footer(payload: dict[str, Any], totals: dict[str, float]) -> str:
    ident = payload["identity"]
    parts = [
        f"Agent: {payload.get('agent_name') or DEFAULT_AGENT_NAME}",
        f"Generated: {payload['generated']}",
        f"Data source: Procore only ({ident.get('project', 'project not named')})",
        f"Package indexed: {ident.get('package_pages', 0)} pp.",
    ]
    limitations = payload.get("limitations")
    if limitations:
        parts.append(f"Limitations: {_ascii(str(limitations))}")
    prov = "\n      ".join(f"<span>{_esc(part)}</span>" for part in parts)
    return f"""<footer>
  <div class="wrap">
    <p class="prov">
      {prov}
    </p>
    <p class="disclaimer">{_esc(CLOSING_LINE)}</p>
  </div>
</footer>"""


def build_data_island(payload: dict[str, Any], totals: dict[str, float]) -> dict[str, Any]:
    """Derive the machine-readable island so it can never disagree with the render."""
    ident = payload["identity"]
    rollup = payload["cost_breakdown"].get("rollup") or {}
    position = rollup.get("position")

    def _grade(value: Any) -> str:
        return value["status"] if isinstance(value, dict) else str(value)

    island: dict[str, Any] = {
        "schema": SCHEMA,
        "co_number": ident["co_number"],
        "subject": ident["subject"],
        "contractor": ident["contractor"],
        "project": ident.get("project"),
        "commitment": ident.get("commitment"),
        "generated": payload["generated"],
        "verdict": payload["verdict"]["token"],
        "pricing_basis": ident.get("pricing_basis"),
        "change_type": payload["review_summary"].get("change_type", {}).get("text")
        if isinstance(payload["review_summary"].get("change_type"), dict)
        else payload["review_summary"].get("change_type"),
        "cor_total": totals["total"],
        "direct_cost_subtotal": totals["direct"],
        "backup_covered": totals["covered"],
        "package_pages": ident.get("package_pages"),
        "pillars": {
            key: {"status": value["status"], "note": _ascii(str(value.get("note") or value["points"][0]))}
            for key, value in payload["pillars"].items()
        },
        "change_event_rom": payload.get("change_event_rom") or {},
        "schedule_claim": payload.get("schedule_claim") or {"claimed": False},
        "line_items": [
            {
                "n": row.get("n"),
                "description": _ascii(str(row["item"])),
                "cost": round(float(row["cost"]), 2),
                "substantiated": _grade(row["substantiated"]),
                "validated": _grade(row["validated"]),
                **({"backup_covered": round(float(row.get("backup_covered", 0) or 0), 2)}
                   if not row.get("derived") else {"derived": True}),
                "pillar": row.get("pillar", "cost"),
            }
            for row in payload["cost_breakdown"]["rows"]
        ],
        "document_gaps": [
            {
                "document": _ascii(str(row["document"])),
                "status": row["status"],
                "supports": _ascii(str(row.get("supports", ""))),
            }
            for row in payload["document_inventory"]["rows"]
            if status_classes(row["status"])[1] == "not-found"
        ],
        "findings": [
            {
                "pillar": finding.get("pillar", "cost"),
                "status": finding["status"],
                "amount": round(float(finding.get("amount", 0) or 0), 2),
                "summary": _ascii(re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", str(finding["text"]))),
            }
            for finding in payload.get("findings") or []
        ],
        "next_steps": [_ascii(str(step)) for step in (payload.get("next_steps") or [])],
        "guardrail": "Analysis only - no approval, rejection, or Procore write-back.",
    }
    rom = island["change_event_rom"]
    if rom.get("amount"):
        rom["delta_vs_cor"] = round(totals["total"] - float(rom["amount"]), 2)
    if position is not None:
        island["recommended_position"] = round(float(position), 2)
        island["recommended_adjustment"] = round(totals["total"] - float(position), 2)
    return island


def render_report(payload: dict[str, Any]) -> str:
    """Render the branded, self-contained COR review report from a payload."""
    for key in ("identity", "generated", "verdict", "review_summary",
                "document_inventory", "cost_breakdown", "pillars"):
        _require(payload, key)

    totals = _cost_totals(payload)
    island = json.dumps(build_data_island(payload, totals), indent=2)

    sections = [_verdict_section(1, payload), _review_summary(2, payload),
                _document_inventory(3, payload), _cost_breakdown(4, payload, totals)]
    for builder in (_findings, _next_steps, _references):
        rendered = builder(len(sections) + 1, payload)
        if rendered:
            sections.append(rendered)

    return (
        f"{_head(payload, totals)}\n<body>\n\n"
        f"{_masthead(payload, totals)}\n\n"
        f"{_deck(payload, totals)}\n\n"
        f'<main class="wrap">\n\n' + "\n\n".join(sections) + "\n\n</main>\n\n"
        f"{_footer(payload, totals)}\n\n"
        f'<script type="application/json" id="cor-review-data">\n{island}\n</script>\n'
        "</body>\n</html>\n"
    )


def render_file(path: str, payload: dict[str, Any]) -> str:
    html_text = render_report(payload)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(html_text)
    return html_text
