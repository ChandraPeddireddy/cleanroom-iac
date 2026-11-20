# Clean Rooms as code (interim Terraform replacement)

Terraform provider v1.92.0 (PR #5072) removed `databricks_clean_rooms_clean_room`,
`databricks_clean_room_asset` and `databricks_clean_room_auto_approval_rule`. Databricks product
guidance is that Terraform support is deprioritized with no ETA. Use the REST API, SDK or CLI
in the meantime.

This tool gives a Terraform-like workflow on top of the REST API: a config file in Git,
`plan` / `apply` / `drift` / `destroy`, idempotent re-runs and drift reporting.

**Step-by-step rollout, permissions, test plan and CI/CD:** see [docs/IMPLEMENTATION_GUIDE.md](docs/IMPLEMENTATION_GUIDE.md).

## Files

| File | Purpose |
|---|---|
| `cleanroom_iac.py` | Engine (stdlib only). Plan/apply/drift/destroy, config validation, SDK and CLI transports, CLI entry point |
| `cleanroom_iac_runner.py` | Databricks notebook that runs the engine as a job or interactively; `display()` output plus an optional Delta run log |
| `config/cleanrooms.example.yaml` | Annotated example config |
| `docs/CleanRooms_IaC_Implementation_Guide.docx` | Word copy of the guide. Regenerate after editing the Markdown: `python3 tools/md2docx.py docs/IMPLEMENTATION_GUIDE.md docs/CleanRooms_IaC_Implementation_Guide.docx "Clean Rooms as Code: Implementation and Test Guide"` |
| `docs/IMPLEMENTATION_GUIDE.md` | Rollout guide: prerequisites, identity setup, jobs, test plan, CI/CD, Terraform coexistence, troubleshooting |
| `tests/` | 62 offline tests: 52 for the engine against an in-memory fake API (`test_cleanroom_iac.py`, `test_cleanroom_iac_edges.py`) and 10 for the Word converter, including a schema-order check (`test_md2docx.py`) |

## What it manages

| Resource | Created | Updated | Deleted | Drift reported |
|---|---|---|---|---|
| Clean room | yes (owner transferred after ACTIVE) | `comment`, `owner` | `destroy` mode only | cloud, region, collaborators, egress mode, `enable_shared_output`. These are immutable, so they're reported but never auto-changed |
| Output catalog | yes | no (immutable) | n/a | name mismatch |
| Assets owned by this metastore (TABLE, VIEW, VOLUME, FOREIGN_TABLE, NOTEBOOK_FILE) | yes | notebook content, runners, description, environment version; table partitions | `prune=true` | unmanaged assets; `PERMISSION_DENIED`; changed `local_name` (replaced only with `allow_replace=true`) |
| Auto-approval rules owned by this metastore | yes | n/a (create/delete) | `prune=true` | unmanaged rules |
| UC grants on the `CLEAN_ROOM` securable (`MODIFY_CLEAN_ROOM`, `EXECUTE_CLEAN_ROOM_TASK`) | yes | yes; authoritative for every principal listed | listed principals: always; others: `prune=true` | unlisted principals |

Assets and rules owned by **other** collaborators are never touched. Set `create: false` on rooms
a partner created and this metastore joined. The tool then manages only this side's assets, rules
and grants.

## Quick start

1. Put this folder in a **Git folder** in the workspace, so the notebook can import `cleanroom_iac.py`.
2. Copy `config/cleanrooms.example.yaml` to `config/cleanrooms.yaml` and fill in the placeholders.
   Get each partner's **sharing identifier** (`cloud:region:metastore-uuid`) from them.
3. Open `cleanroom_iac_runner`, leave `mode=plan`, click Run all, and review the table.
4. Set `mode=apply`. Re-running converges, and a second apply with no config change makes no API writes.
5. Optional: set `state_path`, for example `/Workspace/Shared/cleanroom_iac/state.json`, to detect
   notebook source changes and edits made outside the tool (etag).
6. Optional: set `log_table`, for example `ops.cleanroom_iac.run_log`, to keep an audit trail.

### Run as a job

- Two tasks or two jobs, both on serverless or any UC-enabled compute:
  - **apply**: triggered by CI after merge to the main branch, `mode=apply`
  - **drift**: scheduled daily, `mode=drift`, `fail_on_drift=true`, with failure notifications on
- Run as a **service principal**. Set `max_concurrent_runs = 1`.

### Required permissions (run-as identity)

| Action | Privilege |
|---|---|
| **Membership in the owner group** | **Required.** The room is created as the run-as identity and then handed to `owner`. Without membership, the run-as identity loses the right to manage the room right after the transfer |
| Create a room / get the sharing identifier | `CREATE_CLEAN_ROOM` on the metastore, or metastore admin |
| Update / add assets / rules | room owner or `MODIFY_CLEAN_ROOM` |
| Share a table/view/volume | the **room owner** needs `SELECT`/`READ VOLUME` + `USE CATALOG`/`USE SCHEMA`, and must keep them, or the asset goes to `PERMISSION_DENIED`. Make the owner a group |
| Manage grants | room owner, or `MANAGE` on the room |
| State file | write access to the `state_path` folder |

## Terraform users (`terraform_data` + `local-exec`)

To keep Terraform as the entry point, call the CLI runner. Re-runs are safe because the
engine is idempotent, unlike raw `curl` calls.

```hcl
resource "terraform_data" "clean_rooms" {
  triggers_replace = filesha256("${path.module}/config/cleanrooms.yaml")

  provisioner "local-exec" {
    command = "python3 cleanroom_iac.py --config config/cleanrooms.yaml --mode apply --profile ${var.databricks_profile} --state-path /Workspace/Shared/cleanroom_iac/state.json"
  }
}
```

Limits of this pattern: `terraform plan` won't show clean room changes. Run
`--mode plan` in the pipeline for that. Deletion isn't automatic, so use `--mode destroy`
deliberately. CLI exit codes: `0` success, `1` a change failed, `2` drift found in `drift` mode.

## Verification status

### Verified offline
62 unit tests (`tests/`): 52 run the engine against an in-memory fake of the APIs, 10 cover the Word converter. The engine tests cover:
- create, including the owner transfer after ACTIVE
- idempotent re-apply (zero writes)
- updates, and drift on immutable settings (collaborators, region, egress, shared output)
- detection of notebook source changes and edits made outside the tool
- prune and replace
- a partial failure followed by a re-run that converges
- one failing room not blocking the others
- join-only rooms, FAILED and PROVISIONING rooms, output catalog states, and `PERMISSION_DENIED` assets
- config-driven cloud and region (required, case-insensitive, strings only, inherit from `defaults`, never read from the metastore; `plan` warns without blocking when the cloud differs from the workspace metastore's)
- pagination and URL encoding
- both transports (CLI and SDK error mapping)
- CLI exit codes and the destroy confirmation

### Verified live on the Azure sandbox (Databricks-internal test workspace)
| Check | Result |
|---|---|
| CLI transport: metastore summary, room 404, `CLEAN_ROOM` permissions API, notebook export (HTML + stable SOURCE hash), state file write/read | pass |
| Notebook as a **serverless job**: SDK transport, YAML parsing (PyYAML is in the runtime), widgets, job detection (widgets kept), `display()`, Delta run log | pass |
| `drift` + `fail_on_drift=true` fails the run; `destroy` without the exact confirmation is refused | pass |
| Deleted rooms are still listed with `status=DELETED`, and the engine treats them as absent | confirmed |
| Create request body: the API accepted the structure after the fixes below | pass (up to authorization) |

### Behaviour found live and built in
1. **The create call rejects `owner`** unless it is empty or the caller ("Clean room owner must be empty
   or match the caller"). The engine creates without an owner, then transfers ownership once the room is ACTIVE.
2. **Cloud and region are config-only.** The central clean room's region can differ from the workspace
   metastore's region, and not every region supports Clean Rooms (the API answers "Cloud/region ... is not
   supported"). So the engine never derives cloud or region from the workspace: `cloud_vendor` and
   `region` are required in the config (per room or in `defaults`), checked for format only, and the API
   decides whether they're supported. There is no built-in region list.
3. **A collaborator on the same metastore needs `invite_recipient_email` + `invite_recipient_workspace_id`.**
   Without them the API rejects the request as a second creator. `plan` now reports this before calling the API.

### Not verified (first apply in the customer environment)
Our sandbox returned **"User is not authorized to create a clean room"**, both for a same-metastore collaborator
and for an AWS collaborator, even though effective `CREATE_CLEAN_ROOM` shows through the workspace admins group.
No room was ever created, so these are untested live:
- room reaching ACTIVE, then the owner transfer PATCH
- adding assets. Open question: is a `STREAMING_TABLE` / `MATERIALIZED_VIEW` source shared as `TABLE`?
- auto-approval rules, `CLEAN_ROOM` grants PATCH, output catalog
- notebook update (PATCH) semantics, partitions, and `etag` behaviour
- destroy, and its effect on the other collaborators

**First-run procedure for the customer:** start with a throwaway room (`zz_` prefix) and one small table. Run
`plan`, then `apply`, then `apply` again, which must show no changes. Then `drift`, then `destroy`. Only then
onboard real rooms.

## Run the tests

```bash
python3 -m unittest discover -s tests -v
```
