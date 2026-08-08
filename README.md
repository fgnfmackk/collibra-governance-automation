# collibra-governance-automation

Discover PostgreSQL technical metadata, export a deterministic governance inventory, map it into an inspectable Collibra-oriented desired state, compare against remote managed state, and apply safe plan-driven synchronization through a single CLI.

**Stack:** Python 3.12 · PostgreSQL 16 · Psycopg 3 · httpx · Docker Compose · MIT

**Release prep:** package version `1.0.0` is ready for tagging after merge. See [CHANGELOG.md](CHANGELOG.md). This repository state prepares `v1.0.0`; the Git tag and GitHub Release are created only after merge to `main`.

## What is implemented

```text
PostgreSQL metadata discovery
-> deterministic governance inventory
-> Collibra mapping
-> mock/live adapter boundary
-> deterministic diff
-> safe sync
-> CLI
```

| Area | Status |
| --- | --- |
| PostgreSQL discovery + vendor-neutral model | Real against local demo |
| Deterministic inventory export | Real |
| Collibra mapping + sync planning | Real, local |
| Mock Collibra adapter | Real, process-local, no external network |
| Live Collibra Core REST API v2 adapter | Contract-tested; no commercial tenant validation claimed |
| CLI (`scan` / `export` / `diff` / `sync` / `config validate`) | Real |
| Governance-as-Code (`governance.yaml`, snapshots, content identities) | Real (opt-in via `--config`) |
| Live tenant credentials / commercial validation | Not provided by this repository |

Central safety: sync defaults to dry-run (zero remote mutations). Writes require `--apply`. Live writes additionally require `--confirm-live`. No automatic deletes.

```mermaid
flowchart LR
  PostgreSQL --> Scanner
  Scanner --> GovernanceModel
  GovernanceModel --> MetadataInventory
  GovernanceModel --> CollibraMapping
  CollibraMapping --> DesiredState
  DesiredState --> RemoteState
  RemoteState --> SyncPlan
  SyncPlan --> DryRunOrApply
  DryRunOrApply --> MockAdapter
  DryRunOrApply --> LiveCoreRESTv2Adapter
```

Neutral core owns discovery, inventory, mapping, and plan construction. Adapters are the integration boundary. Plans are built before any write.

## Quick start (clean checkout, mock)

No Collibra tenant is required.

### Bash / Linux / macOS / WSL / Git Bash

```bash
git clone https://github.com/fgnfmackk/collibra-governance-automation.git
cd collibra-governance-automation
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
docker compose up -d --wait
governance scan
governance export
governance diff --mode mock
governance sync --mode mock
governance sync --mode mock --apply
docker compose down -v
```

### PowerShell / Windows

```powershell
git clone https://github.com/fgnfmackk/collibra-governance-automation.git
cd collibra-governance-automation
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
docker compose up -d --wait
governance scan
governance export
governance diff --mode mock
governance sync --mode mock
governance sync --mode mock --apply
docker compose down -v
```

Copy `.env.example` to `.env` only when local overrides are needed. Defaults match the Docker Compose demo. Collibra defaults to mock mode.

Optional Bash helper for the SQL demo contract: `bash sample/verify_demo.sh` (Bash / WSL / Git Bash).

## CLI

```text
governance scan [--config PATH] [--profile NAME]
governance export [--config PATH] [--profile NAME] [--artifact inventory|snapshot] [--output PATH]
governance diff [--config PATH] [--profile NAME] [--mode mock|live] [--mapping-config PATH] [--json]
governance sync [--config PATH] [--profile NAME] [--mode mock|live] [--mapping-config PATH] [--apply] [--confirm-live] [--json]
governance config validate [--config PATH] [--profile NAME] [--json]
governance check --config PATH [--profile NAME] [--format human|json]
governance plan --config PATH --output FILE.gplan [--profile NAME] [--format human|json]
governance plan inspect FILE.gplan [--format human|json]
governance apply FILE.gplan --config PATH [--profile NAME] [--format human|json] [--apply] [--confirm-live]
governance --help
governance --version
```

Without `--config`, legacy operational commands keep the v1.0 environment-based settings path. YAML is never auto-discovered from the working directory. New GaC commands `check`, `plan`, and `apply` require explicit `--config` (except `plan inspect`).

## Governance-as-Code (optional)

Declare sources, optional Collibra targets, artifact paths, and policy file hooks in `governance.yaml`. See [`sample/governance.example.yaml`](sample/governance.example.yaml).

```bash
export DATABASE_URL="${DATABASE_URL:-postgresql://postgres:postgres@localhost:5432/governance_demo}"
export COLLIBRA_MODE="${COLLIBRA_MODE:-mock}"

governance config validate --config sample/governance.example.yaml
governance config validate --config sample/governance.example.yaml --json
governance export --config sample/governance.example.yaml --artifact snapshot

# Policy evaluation (no Collibra I/O)
governance check --config sample/governance.example.yaml
governance check --config sample/governance.example.yaml --format json

# Plan-review-apply (saved .gplan; apply executes the reviewed action set)
governance plan --config sample/governance.example.yaml --output production.gplan
governance plan inspect production.gplan
governance apply production.gplan --config sample/governance.example.yaml
# Mutations still require --apply; live apply still requires --confirm-live
```

Validation covers YAML parse, JSON Schema, profile overlay, and semantic checks before any PostgreSQL or Collibra I/O. Secrets stay in environment variables via `*_env` references. Snapshots are a distinct artifact from the metadata inventory and embed a required `content_identity`. Component identities (`config` / `snapshot` / `mapping` / `policy` / `remote_state` / `target_context` / plan) are integrity digests, not authenticity proofs.

`governance plan` / `governance apply` use `--config` and optional `--profile` as the declarative source of truth (no `--mode` / `--mapping-config` overrides on that path). Legacy `diff` / `sync` keep their existing overrides. YAML and `.gplan` never authorize writes. Remote apply is fail-fast and not a distributed transaction.

Exit codes (legacy commands `scan` / `export` / `diff` / `sync` / `config validate`):

- `0` — completed successfully
- `1` — operational / configuration / integration failure
- `2` — usage / argument / live-apply confirmation error

Additional exit codes for `check` / `plan` / `apply` only:

- `3` — policy error violations (or plan blocked by policy errors)
- `4` — config / runtime-resolution / policy / saved-plan validation failure
- `5` — stale saved plan (apply refused before mutation)

`create` / `update` / `unchanged` / `remote_only` counts are plan actions, not completed remote writes. Dry-run means zero remote mutations (`applied=0`); it does not mean zero network in live mode.

### Representative local output (mock demo)

```text
governance 1.0.0
```

```text
source=governance-demo
database=governance_demo
schemas=2
tables=7
columns=41
primary_keys=7
foreign_keys=7
relationships=7
```

```text
mode=mock
create=108
update=0
unchanged=0
remote_only=0
writes=0
CREATE column col:governance-demo/governance_demo/commerce/customers/customer_id
CREATE table tbl:governance-demo/governance_demo/commerce/customers
CREATE relationship rel:fk:tbl:governance-demo/governance_demo/commerce/orders/orders_customer_id_fkey
```

A standalone mock `diff` starts from an empty mock adapter, so the plan commonly lists `CREATE` actions for the desired state. Mock state is process-local and is not persisted between CLI invocations.

```text
mode=mock
dry_run=true
create=108
update=0
unchanged=0
remote_only=0
applied=0
success=true
```

## Mock vs live

### Mock

- Zero external network
- Uses symbolic `mock:*` mapping refs
- No Collibra credentials
- Suitable for Quick Start and CI

### Live

- Uses existing Settings/env auth (`COLLIBRA_BASE_URL` plus Basic or Bearer)
- Requires `--mapping-config PATH` with tenant catalog refs only (no credentials in that file)
- `diff` and dry-run `sync` may perform GET/read calls against remote managed state
- Remote mutations require both `--apply` and `--confirm-live`
- Contract-tested; commercial tenant validation is not claimed

Example mapping shape: [`sample/collibra-mapping.example.json`](sample/collibra-mapping.example.json). Placeholders such as `<tenant-domain-id>` are intentionally non-functional and are rejected by the live CLI until replaced.

## Live usage

Configure auth in the environment (not on the command line):

```text
COLLIBRA_MODE=live
COLLIBRA_BASE_URL=https://your-collibra-host.example
COLLIBRA_USERNAME=...
COLLIBRA_PASSWORD=...
# or COLLIBRA_BEARER_TOKEN=...
```

Use a real mapping file derived from the sample placeholders:

```bash
governance diff \
  --mode live \
  --mapping-config tenant-mapping.json

governance sync \
  --mode live \
  --mapping-config tenant-mapping.json

governance sync \
  --mode live \
  --mapping-config tenant-mapping.json \
  --apply \
  --confirm-live
```

Dry-run first. Live dry-run may read remote state; it does not POST/PATCH/DELETE.

## PostgreSQL demo

```bash
docker compose up -d --wait
```

Local credentials are fictional and development-only:

| Setting | Value |
| --- | --- |
| Host | `localhost` |
| Port | `5432` |
| Database | `governance_demo` |
| User | `postgres` |
| Password | `postgres` |
| Logical source | `governance-demo` |

Primary demo schema: `commerce` (`customers`, `products`, `employees`, `orders`, `order_items`, `payments`, `marketing_contacts`). Seed data is synthetic (`@example.com`).

## Library seams

The CLI orchestrates the same public layers available to library callers:

```python
from governance.config import load_settings
from governance.exporters import MetadataInventory, write_inventory
from governance.integrations.collibra import (
    build_collibra_adapter,
    build_sync_plan,
    execute_sync_plan,
    map_to_desired_state,
    mock_mapping_config,
)
from governance.scanner import PostgresMetadataScanner

settings = load_settings()
model = PostgresMetadataScanner(settings).scan()
inventory = MetadataInventory.from_model(model)
write_inventory(inventory, settings.inventory_output_path)

desired = map_to_desired_state(model, mock_mapping_config())
adapter = build_collibra_adapter(settings, mock_mapping_config())
remote = adapter.read_remote_state(desired)
plan = build_sync_plan(desired, remote)
dry_run = execute_sync_plan(adapter, plan, apply=False)
```

## GitHub Action (Governance as Code)

Official composite Action at the repository root (`action.yml`). Supported runners for v1: **GitHub-hosted Linux/Ubuntu** only.

The Action installs this package into a fresh Action-owned virtualenv under `RUNNER_TEMP`, then runs the governance CLI with isolated Python (`python -I -m ...`). Consumer site-packages are not modified. Relative config paths resolve against `GITHUB_WORKSPACE` (the consumer must checkout their repository before `uses:`).

**Writes performed: always 0.** The Action never calls `governance apply` or mutating `sync`.

### Inputs (v1)

| Input | Default | Notes |
| --- | --- | --- |
| `config` | required | Workspace-relative path to `governance.yaml` |
| `profile` | `""` | Forwarded as `--profile` when non-empty |
| `operation` | `plan` | `validate` \| `check` \| `plan` (Action mode, not Collibra `--mode`) |
| `output-format` | `human` | Console only (`human` \| `json`); does not change artifacts or step summary |
| `fail-on-policy-error` | `"true"` | Final gate exits `3` when status is blocked |
| `output-directory` | `.governance` | Artifact root; must stay inside the workspace |
| `plan-path` | `.governance/governance.gplan` | Must be under `output-directory` |
| `pr-comment` | `"false"` | Opt-in sticky PR comment |
| `github-token` | `""` | Comment step only; never passed to governance CLI |

Provider credentials are **not** Action inputs. They remain environment variables referenced by governance.yaml `*_env` keys.

### A. Fork-safe validation

```yaml
name: Governance validate
on: pull_request
permissions:
  contents: read
jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: fgnfmackk/collibra-governance-automation@<commit-sha>
        with:
          config: governance.yaml
          operation: validate
          pr-comment: "false"
```

Do not use `pull_request_target` with an untrusted PR checkout and secrets.

### B. Governance check

`operation: check` evaluates policies against a PostgreSQL source. Set `DATABASE_URL` (or the discrete connection env vars from your config). No Collibra I/O is required for check.

### C. Trusted deterministic planning

For live remote reads, inject provider env vars only after an explicit same-repository guard:

```yaml
- name: Plan (same repository only)
  if: github.event.pull_request.head.repo.full_name == github.repository
  uses: fgnfmackk/collibra-governance-automation@<commit-sha>
  with:
    config: governance.yaml
    operation: plan
    pr-comment: "false"
  env:
    DATABASE_URL: ${{ secrets.DATABASE_URL }}
    COLLIBRA_MODE: mock
    # Live reads: COLLIBRA_BASE_URL / credentials via env refs in governance.yaml
```

Still zero remote writes.

### D. Artifact upload

```yaml
- uses: actions/upload-artifact@v4
  if: always()
  with:
    name: governance-action
    path: ${{ steps.gac.outputs.artifacts-path }}
```

Prefer uploading the Action `artifacts-path` directory so a missing `.gplan` on blocked runs does not break the upload. Pin `upload-artifact` by full commit SHA in production workflows.

### E. Optional sticky comment

```yaml
permissions:
  contents: read
  pull-requests: write
# ...
- uses: fgnfmackk/collibra-governance-automation@<commit-sha>
  with:
    config: governance.yaml
    operation: plan
    pr-comment: "true"
    github-token: ${{ github.token }}
```

Fork PRs skip commenting (`comment-status=skipped_untrusted_fork`). Missing token on a trusted PR fails the Action with `comment-status=failed` after governance artifacts are written. The Action uses `GITHUB_API_URL` for GitHub Enterprise Server compatibility.

### F. Version pinning

- Strongest: pin the Action to a full immutable commit SHA.
- After a separate release that bumps the package to `1.1.0` and creates tag `v1.1.0`, consumers may pin that immutable SemVer tag.
- Do not use mutable `@main`.
- This repository state remains package `1.0.0`; it does not create release tags.
- The Action ref pins Action metadata and the Python package installed from `GITHUB_ACTION_PATH` together.

## Repository structure

```text
action.yml                  official composite GitHub Action
src/governance/
  domain/                   vendor-neutral governance model
  scanner/                  PostgreSQL metadata discovery
  exporters/                deterministic inventory JSON
  integrations/collibra/    mapping, adapters, sync planning
  github_ci/                Action runner, reporting, sticky comments
  cli.py                    argparse CLI orchestration
tests/
sample/                     demo SQL, verification helper, mapping example
.github/workflows/          CI quality gates
```

## Testing and CI

```bash
ruff check src tests
ruff format --check src tests
pytest -m "not integration and not collibra_integration and not cli_integration"
python -m build
```

With the demo database running:

```bash
pytest -m integration
pytest -m collibra_integration
pytest -m cli_integration
docker compose down -v
```

CI defines seven `ubuntu-latest` jobs:

- `lint`
- `unit-tests`
- `package-validation`
- `postgres-integration`
- `metadata-integration`
- `collibra-integration`
- `cli-integration` (includes official Action `uses: ./` PASS and BLOCKED smokes)

No commercial Collibra tenant, self-hosted runners, or OS matrix is required.

## Limitations

- No commercial Collibra tenant validation
- No OAuth acquisition/refresh
- No automatic deletes
- No arbitrary tenant customization beyond configured refs
- No transactional REST snapshot across concurrent mutations
- No large-scale performance benchmark
- Mock state is process-local demonstration state
- Demo data is fictional

## License

[MIT](LICENSE)
