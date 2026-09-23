# datagrid-agents

Construction-focused AI agents built on the [Datagrid API](https://developers.datagrid.com).

This repo gives you ready-made agent blueprints (RFI review, submittals, safety, schedule risk, daily reports, change orders), a small Python CLI to create/sync them in your Datagrid workspace, and examples for running prompts through Converse.

## Prerequisites

1. A Datagrid account and API key from [app.datagrid.com](https://app.datagrid.com) (API Keys).
2. Python 3.10+.
3. Node.js 20+ (for the Lessons Learned web app).

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pip install -r server/requirements.txt
cp .env.example .env
# edit .env and set DATAGRID_API_KEY
```

Optional: set `CONSTRUCTION_KNOWLEDGE_IDS` to a comma-separated list of Datagrid knowledge IDs so agents are scoped to your project docs instead of all org knowledge.

## Lessons Learned web app

Monochrome interview UI focused only on lessons extraction:

**Confirm project in Datagrid knowledge → scope-narrowing questions → 20 correlative analysis calls via orchestrator fan-out** with live generative reasoning, elapsed timer, and project connectivity graph → buried-pattern aggregate **top-50 findings table** → follow-up Q&A

```bash
# terminal 1 — API
source .venv/bin/activate
uvicorn server.app:app --reload --port 8000

# terminal 2 — UI
cd web
npm install
npm run dev
```

Open http://localhost:5173 — Vite proxies `/api` to the FastAPI server.

## Quick start

List the local construction agent blueprints:

```bash
datagrid-agents list
# or: python -m datagrid_agents.cli list
```

Create them in your Datagrid organization (IDs are saved to `.agent_ids.json`):

```bash
datagrid-agents create
# or one agent:
datagrid-agents create rfi_reviewer
```

Run an agent (uses the definition's sample prompt if you omit `--prompt`):

```bash
datagrid-agents run rfi_reviewer
datagrid-agents run rfi_reviewer -p "Review RFI-12 for missing drawing references."
```

Update agents after editing YAML definitions:

```bash
datagrid-agents sync
```

List agents already in Datagrid:

```bash
datagrid-agents remote
```

## Cursor orchestrator

Stdlib Datagrid API toolkit: explore a teamspace, write targeted prompts, then
dispatch many converse jobs concurrently with retry-on-stall.

```bash
python .cursor/skills/datagrid-orchestrator/scripts/datagrid_client.py whoami
python .cursor/skills/datagrid-orchestrator/scripts/explore.py --teamspace "KSA Demo" --out profile
python .cursor/skills/datagrid-orchestrator/scripts/orchestrate.py \
  --agents "Mentor Agent,Schedule Intelligence" \
  --prompt "Top risks from lessons learned" \
  --teamspace "KSA Demo" --out results --concurrency 6
```

Skill: `.cursor/skills/datagrid-orchestrator/` (`/datagrid-orchestrator`).
Subagent: `.cursor/agents/datagrid.md`.

The `datagrid-agents` CLI now uses the same stdlib orchestrator:

```bash
datagrid-agents whoami
datagrid-agents roles
datagrid-agents explore --teamspace "KSA Demo" --out profile
datagrid-agents orchestrate \
  --agents "Mentor Agent,Schedule Intelligence" \
  --prompt "Top risks from lessons learned" \
  --teamspace "KSA Demo" --out results
```

`src/datagrid_agents/orchestrator/` is a thin adapter: role registry (`agents.yaml`),
`run_parallel` (stall retries via the skill), and the lessons-multipass lenses used
by the Lessons Learned web app.

## Included agents

| Slug | Use case |
| --- | --- |
| `rfi_reviewer` | Completeness / clarity checks before RFIs go to design |
| `submittal_checker` | Spec vs product data review and disposition notes |
| `safety_observer` | Hazard spotting from reports/photos with corrective actions |
| `schedule_risk` | Critical-path and delay risk analysis |
| `daily_report_summarizer` | Field report → PM/owner summary |
| `change_order_analyst` | COR documentation and pricing gap review |
| `cor_review_agent` | Micron owner-side COR review → branded HTML report (cost / schedule / technical) |

Definitions live in `src/datagrid_agents/definitions/*.yaml`. Each file sets:

- `system_prompt` — role and scope
- `custom_prompt` — response format
- `planning_prompt` — multi-step approach
- `tools` — Datagrid tools (e.g. `semantic_search`, `pdf_extraction`)
- `agent_model` — defaults to `magpie-2.5` (Execute tier)
- `llm_model` — optional; pins the underlying model. Datagrid defaults to a lite model, which is
  fine for short answers but drops long render contracts, so `cor_review_agent` pins
  `claude-opus-4-8`

Long prompts can live outside the YAML: use `system_prompt_file`, `custom_prompt_file`, or
`planning_prompt_file` with a path relative to `src/datagrid_agents/`, and inline other files
into them with a `{{include: relative/path}}` line. `cor_review_agent` uses both so its
render contract is the shipped HTML template rather than a copy pasted into a prompt.

## COR review report (HTML)

`cor_review_agent` answers a review request with one self-contained, Procore-branded HTML
document instead of chat tables: verdict deck, cost/schedule/technical pillar cards, document
inventory, per-line cost validation with live Procore and Datagrid links, findings, next steps,
and a JSON data island so reasoning models can read the same figures.

```bash
datagrid-agents report sample --out cor_review_sample.html   # populated mockup
datagrid-agents report template --out cor_review_template.html  # skeleton the agent fills
datagrid-agents report validate cor_review_sample.html       # check the render contract
```

`report validate` enforces what can be checked mechanically — required sections, the status
vocabulary (`Validated` / `Partial` / `Not validated` / `Not found in Procore`), no empty table
cells, Procore/Datagrid-only links, a self-contained document, the exact closing disclaimer, and
arithmetic that reconciles against the data island. Use it on agent output before it goes to a
change manager.

The template is inlined into the agent's custom prompt with `{{include:}}`, so the render contract
the agent is given is the shipped file — edit the template and the prompt follows on the next
`datagrid-agents sync`. Assets:

```text
src/datagrid_agents/reports/templates/cor_review_report.html  # branding + render contract
src/datagrid_agents/reports/samples/cor_review_report_sample.html
src/datagrid_agents/prompts/cor_review_agent/{system,planning,custom}.md
```

## Add your own agent

1. Copy an existing YAML file in `src/datagrid_agents/definitions/`.
2. Change the filename slug, name, prompts, and tools.
3. Run `datagrid-agents sync <slug>`.
4. Run `datagrid-agents run <slug> -p "..."`.

Or draft from natural language with Datagrid's generate flow:

```bash
python examples/generate_agent_from_prompt.py "An agent that reviews punch lists by trade"
```

## Examples

- `examples/create_and_run_rfi_agent.py` — sync + converse for the RFI agent
- `examples/generate_agent_from_prompt.py` — generate → claim → create

## Project layout

```text
src/datagrid_agents/
  cli.py                 # datagrid-agents command
  client.py              # Datagrid SDK helper
  registry.py            # load YAML definitions
  service.py             # create / sync / converse
  definitions/           # construction agent blueprints
  prompts/               # long-form prompt bodies (markdown, with {{include:}})
  reports/               # COR review HTML template, sample, and validator
  orchestrator/          # stdlib skill adapter + lessons-multipass lenses
server/
  app.py                 # Lessons Learned FastAPI
web/                     # Lessons Learned Vite/React UI
.cursor/skills/
  datagrid-orchestrator/ # stdlib API explore + concurrent converse
.cursor/agents/
  datagrid.md            # Cursor Datagrid subagent
examples/
tests/
```

## Tips for production use

- Scope knowledge with `corpus` / `CONSTRUCTION_KNOWLEDGE_IDS` so agents only read project documents.
- Prefer least-privilege tools; start narrow and add `pdf_extraction`, `data_analysis`, etc. only when needed.
- Use `chat_mode=full_agent` for multi-step tool work; use `light_agent` for faster RAG-style answers.
- Iterate prompts the same way you iterate code: run real RFIs/submittals, then refine YAML and `sync`.

## Docs

- [Datagrid quickstart](https://developers.datagrid.com/introduction/quickstart)
- [Getting started with Agents](https://developers.datagrid.com/api-reference/agents/agents)
- [Agent best practices](https://developers.datagrid.com/api-reference/agents/agent-best-practices)
- [Converse](https://developers.datagrid.com/api-reference/converse/converse-getting-started)
- Python SDK: [`datagrid_ai`](https://github.com/DatagridAI/datagrid-python)

## Tests

```bash
pip install -e ".[dev]"
pytest
```
