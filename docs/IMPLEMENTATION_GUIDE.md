# Clean Rooms as Code: Implementation and Test Guide

**Audience:** Platform and data engineering teams (Azure Databricks)
**Purpose:** Manage Databricks Clean Rooms from Git while the Terraform resources are unavailable
**Repo:** `cleanroom-iac` (engine `cleanroom_iac.py`, notebook `cleanroom_iac_runner`)

---

## 1. Background and approach

Terraform provider **v1.92.0** (PR #5072) removed these resources:

| Removed Terraform resource | Replaced here by (config section) |
|---|---|
| `databricks_clean_rooms_clean_room` | `clean_rooms[]` (name, collaborators, region, comment, owner, egress, output catalog) |
| `databricks_clean_room_asset` | `clean_rooms[].assets[]` (TABLE, VIEW, VOLUME, FOREIGN_TABLE, NOTEBOOK_FILE) |
| `databricks_clean_room_auto_approval_rule` | `clean_rooms[].auto_approval_rules[]` |
| `databricks_grants` on a clean room (`MODIFY_CLEAN_ROOM`, `EXECUTE_CLEAN_ROOM_TASK`) | `clean_rooms[].grants[]` |

Databricks guidance: Terraform support for Clean Rooms is deprioritized with no published timeline.
Use the REST API, SDK or CLI. This repo wraps the REST API in a Terraform-like workflow:

```
Git (YAML config) --> plan (read-only diff) --> review --> apply (idempotent) --> drift (scheduled check)
```

| Terraform concept | This tool |
|---|---|
| `.tf` files | `config/<env>.yaml` |
| `terraform plan` | `mode=plan` |
| `terraform apply` | `mode=apply` (re-runnable; a second apply makes no changes) |
| drift / refresh | `mode=drift` + `fail_on_drift=true` (fails the job when live differs) |
| `terraform destroy` | `mode=destroy` + typed confirmation |
| state file | live API is the source of truth; an optional workspace JSON file tracks notebook versions only |

What it never does on its own: recreate a room, change immutable settings (collaborators, region,
egress), or touch assets and rules owned by the partner. It reports these as `DRIFT` for a human to act on.

---

## 2. Prerequisites checklist

Complete every item before step 4. Each has a check command (Databricks CLI, run as an admin).

| # | Requirement | How to check | Expected |
|---|---|---|---|
| P1 | Unity Catalog workspace on Azure | `databricks metastores summary -o json` | returns `global_metastore_id` like `azure:<region>:<uuid>` |
| P2 | Delta Sharing enabled on the metastore | same output | `delta_sharing_scope` = `INTERNAL_AND_EXTERNAL` |
| P3 | Serverless compute enabled in the workspace (Clean Rooms runs on serverless) | Workspace admin settings | enabled |
| P4 | Clean Rooms available to the workspace | `databricks clean-rooms list` | returns a list (an empty `[]` is fine), not an error |
| P5 | A **supported central clean room region** chosen | Create Clean Room UI region picker | e.g. `eastus`, `westus`, `westeurope`. **`eastus2` is not supported** for the central room, even if the metastore is in eastus2 |
| P6 | Partner's **sharing identifier** (their global metastore ID) | Partner copies it from Catalog Explorer > Clean Rooms | `cloud:region:metastore-uuid` |
| P7 | Python 3.9+ and Databricks CLI on the CI runner (only if using CI or Terraform) | `databricks --version` | installed and authenticated |

Supported Azure central regions (from Microsoft Learn, November 2026): eastus, westus, canadacentral,
brazilsouth, northeurope, westeurope, germanywestcentral, australiaeast, centralindia, japaneast,
southeastasia. Source: https://learn.microsoft.com/en-us/azure/databricks/clean-rooms/create-clean-room.
The region picker in the UI is authoritative.

> **Important (seen in testing):** a user can hold `CREATE_CLEAN_ROOM` and still get
> "User is not authorized to create a clean room" if Clean Rooms is not enabled for the
> workspace or account. If P4 or the first create fails this way, open a ticket with your
> Databricks account team before continuing.

---

## 3. Identity and permissions setup (one time per environment)

### 3.1 Create the principals

| Principal | Type | Purpose |
|---|---|---|
| `sp-cleanroom-iac-<env>` | Service principal (Entra ID) | Runs the jobs. Never use a personal account |
| `grp-cleanroom-owners-<env>` | Account group | **Owner** of every clean room. The tool transfers ownership to it after create |
| `grp-cleanroom-engineers-<env>` | Account group | Gets `MODIFY_CLEAN_ROOM` (manage assets and notebooks) |
| `grp-cleanroom-analysts-<env>` | Account group | Gets `EXECUTE_CLEAN_ROOM_TASK` (run approved notebooks) |

### 3.2 Grant privileges (a metastore admin runs this SQL)

```sql
-- Allow the service principal to create clean rooms
GRANT CREATE CLEAN ROOM ON METASTORE TO `<sp-application-id>`;

-- The OWNER GROUP must be able to read every object shared into a clean room, and keep that
-- access. If it is revoked, the asset goes to PERMISSION_DENIED.
GRANT USE CATALOG ON CATALOG <catalog>              TO `grp-cleanroom-owners-<env>`;
GRANT USE SCHEMA  ON SCHEMA  <catalog>.<schema>     TO `grp-cleanroom-owners-<env>`;
GRANT SELECT      ON TABLE   <catalog>.<schema>.<t> TO `grp-cleanroom-owners-<env>`;
-- For volumes: GRANT READ VOLUME ON VOLUME <catalog>.<schema>.<v> TO `grp-cleanroom-owners-<env>`;
```

### 3.3 Mandatory group membership

**Add the service principal to `grp-cleanroom-owners-<env>`.**
The create API accepts only the caller as owner, so the tool creates the room as the service
principal and then transfers ownership to the owner group. If the service principal is not in that
group, it loses the right to add assets, rules and grants immediately after the transfer.

### 3.4 Workspace access for the service principal

- `CAN MANAGE` on the Git folder (step 4) and on the state-file folder, e.g. `/Workspace/Shared/cleanroom_iac/`
- `CAN READ` on every notebook that will be shared into a clean room
- If you use the run log: `USE CATALOG`, `USE SCHEMA` and `CREATE TABLE` on the log schema

---

## 4. Get the code into your environment

1. **Mirror the repo into your organization's Git** (Azure DevOps or GitHub Enterprise). Your organization owns the copy from then on.
2. **Create a Git folder** in each workspace: Workspace > Create > Git folder > your repo URL, branch `main`.
   Recommended path: `/Workspace/Shared/cleanroom_iac` (or a service principal-owned folder).
3. **Create one config per environment** from the example:
   ```bash
   cp config/cleanrooms.example.yaml config/dev.yaml
   cp config/cleanrooms.example.yaml config/prod.yaml
   ```
   Commit them. Environment configs belong in Git; only `config/*.local.yaml` is ignored.
4. **Branch protection** on `main`: require a pull request review, plus the CI checks from step 8.

Repo layout:

```
cleanroom_iac.py            engine (stdlib only); also a CLI: python3 cleanroom_iac.py --help
cleanroom_iac_runner        notebook entry point (widgets / job parameters)
config/<env>.yaml           desired state per environment
tests/                      offline unit tests (no workspace needed)
docs/IMPLEMENTATION_GUIDE.md  this guide
```

---

## 5. Write the configuration

Minimal room (replace the `<...>` values):

```yaml
version: 1
defaults:
  owner: "grp-cleanroom-owners-dev"
  wait_timeout_minutes: 30

clean_rooms:
  - name: acme_partner_measurement          # letters, digits, _ and -
    comment: "Managed by cleanroom_iac - do not edit in UI"
    local_alias: acme                       # your alias inside the room (immutable)
    region: eastus                          # supported CENTRAL region (not eastus2)
    collaborators:
      - alias: partner
        global_metastore_id: "azure:<region>:<partner-metastore-uuid>"   # partner sharing identifier
    assets:
      - type: TABLE
        local_name: "<catalog>.<schema>.<table>"   # shared as acme.<schema>.<table>
      - type: NOTEBOOK_FILE
        name: overlap_analysis
        workspace_path: "/Workspace/Shared/cleanroom_notebooks/overlap_analysis"
        runner_aliases: [partner]
    auto_approval_rules:
      - author_alias: partner
        runner_alias: acme
    grants:
      - principal: "grp-cleanroom-engineers-dev"
        privileges: [MODIFY_CLEAN_ROOM]
      - principal: "grp-cleanroom-analysts-dev"
        privileges: [EXECUTE_CLEAN_ROOM_TASK]
```

Field rules:

| Field | Mutable after create? | Notes |
|---|---|---|
| `name`, `local_alias`, `region`, `collaborators`, `egress_network_policy` | **No** | A change shows as `DRIFT`. It needs a new room (new name) |
| `comment`, `owner` | Yes | Updated by `apply` |
| `assets`, `auto_approval_rules`, `grants` | Yes | Created, updated, or deleted (with `prune`) by `apply` |
| `output_catalog` | Create once | Name can't change after creation |
| `create: false` | n/a | Use for rooms a **partner** created. The tool then manages only your assets, rules and grants |
| collaborator on the **same** metastore | n/a | Must also set `invite_recipient_email` and `invite_recipient_workspace_id` |

`plan` validates the config first and lists **every** problem in one error, for example a bad
metastore ID format, unknown aliases, duplicate assets or a missing region.

---

## 6. Run it

### 6.1 Interactively (first time, dev)

Open `cleanroom_iac_runner` in the Git folder and set the widgets:

| Widget | Value |
|---|---|
| `config_path` | `config/dev.yaml` |
| `mode` | `plan` |
| `state_path` | `/Workspace/Shared/cleanroom_iac/state/dev.json` |
| `log_table` | `<catalog>.<schema>.cleanroom_iac_run_log` (optional) |

Run all. Review the table: `CREATE` / `UPDATE` / `DELETE` = changes; `DRIFT` = needs a human; `WARN` = read the detail.

### 6.2 As jobs (all environments)

Create two jobs per environment, both **serverless**, **run as** `sp-cleanroom-iac-<env>`,
`max_concurrent_runs: 1`:

```json
{
  "name": "cleanroom-iac-apply-dev",
  "max_concurrent_runs": 1,
  "run_as": {"service_principal_name": "<sp-application-id>"},
  "tasks": [{
    "task_key": "apply",
    "notebook_task": {
      "notebook_path": "/Workspace/Shared/cleanroom_iac/cleanroom_iac_runner",
      "base_parameters": {
        "config_path": "config/dev.yaml",
        "mode": "apply",
        "state_path": "/Workspace/Shared/cleanroom_iac/state/dev.json",
        "log_table": "<catalog>.<schema>.cleanroom_iac_run_log"
      }
    }
  }],
  "email_notifications": {"on_failure": ["<team-dl@your-company.com>"]}
}
```

The **drift** job is identical except: name `cleanroom-iac-drift-dev`, `"mode": "drift"`,
add `"fail_on_drift": "true"`, and a daily schedule:
`"schedule": {"quartz_cron_expression": "0 0 6 * * ?", "timezone_id": "America/New_York"}`.

Create them with `databricks jobs create --json @job.json`.

Running the notebook from a workspace folder as a serverless job was tested. Running it through a
job `git_source` was not; if you prefer that, test it in dev first.

### 6.3 From CI or Terraform (optional)

```bash
python3 cleanroom_iac.py --config config/dev.yaml --mode plan  --profile <profile>
python3 cleanroom_iac.py --config config/dev.yaml --mode apply --profile <profile> \
        --state-path /Workspace/Shared/cleanroom_iac/state/dev.json
```

Exit codes: `0` OK, `1` a change failed, `2` drift found (`--mode drift`).

---

## 7. Test plan in your environment

Run in **dev** with a throwaway room named `zz_cr_iac_smoke` and one small, non-sensitive table.
Record the result of every step. Don't onboard real rooms until T1–T12 pass.

### 7.0 Offline (any laptop or CI, no workspace)

```bash
python3 -m unittest discover -s tests -v
```
Expected: `OK` (45 tests; one YAML test is skipped if PyYAML isn't installed).

### 7.1 Live smoke test

| # | Step | How | Expected result | Pass |
|---|---|---|---|---|
| T1 | Preflight | Prerequisites P1–P6 | all as expected | [ ] |
| T2 | Config validation | Put a typo in a collaborator metastore ID, run `plan` | run fails listing the error; nothing created | [ ] |
| T3 | Plan | Correct config, `mode=plan` | room, asset, rule and grants show `CREATE`; nothing created (check `databricks clean-rooms list`) | [ ] |
| T4 | Apply (create) | `mode=apply` | room reaches ACTIVE (several minutes); owner becomes the owner group; asset, rule and grants `APPLIED` | [ ] |
| T5 | Idempotency | `mode=apply` again | every row `NOOP`/`SKIPPED`, no writes | [ ] |
| T6 | Drift detection | In the UI, change the room comment, then `mode=drift`, `fail_on_drift=true` | job **fails** with "Drift detected"; the comment shows `UPDATE` | [ ] |
| T7 | Drift repair | `mode=apply` | comment restored; next `plan` is all `NOOP` | [ ] |
| T8 | Notebook update | Edit the shared notebook source, then `plan`, then `apply` | `UPDATE (source notebook changed)` then `APPLIED`; partner sees the new version (approval may be needed) | [ ] |
| T9 | Out-of-band edit | Upload a new notebook version through the UI, then `plan` | `UPDATE (edited outside this tool)` | [ ] |
| T10 | Unmanaged asset | Add a second table in the UI, then `plan`; then `apply` with `prune=true` | first `DRIFT (not in config)`, then `DELETE` → `APPLIED` | [ ] |
| T11 | Grants | Remove a principal from the config, then `apply` with `prune=true` | the principal's privileges removed; owner untouched | [ ] |
| T12 | Destroy guard and destroy | `mode=destroy` with `confirm_destroy=yes`, then `confirm_destroy=zz_cr_iac_smoke` | first run refused; second deletes the room; another `destroy` says `already absent` | [ ] |

### 7.2 Open questions to settle during the smoke test

| Question | How to test | Record |
|---|---|---|
| Can a **streaming table** or **materialized view** be shared, and with which `type`? | Add one as `type: TABLE`; if rejected, try `VIEW` | |
| Does the partner see `T8` updates immediately, or does an approval apply? | Check from the partner workspace | |
| What does `destroy` do for the partner's copy of the room? | Check the partner's clean room list after T12 | |
| Does the owner transfer (T4) keep the service principal able to manage? | T5 must pass after T4 | |

---

## 8. CI/CD pipeline (recommended)

| Trigger | Stage | Command | Gate |
|---|---|---|---|
| Pull request | Unit tests | `python3 -m unittest discover -s tests` | must pass |
| Pull request | Plan (dev and prod) | `python3 cleanroom_iac.py --config config/<env>.yaml --mode plan --profile <env>` | post the output to the PR; a reviewer approves the diff |
| Merge to `main` | Apply dev | `databricks jobs run-now <apply-dev-job-id>` | job must succeed |
| Manual approval | Apply prod | `databricks jobs run-now <apply-prod-job-id>` | change ticket reference |
| Daily | Drift | scheduled drift job | on failure: alert + ticket |

CI authentication: use the CI platform's OIDC/workload identity federation or a Databricks OAuth
service principal secret stored in the CI secret store. Never use a PAT checked into the repo. The
tool itself never reads tokens; it uses the CLI profile or the job's run-as identity.

---

## 9. Day-2 operations

| Task | Change | Run |
|---|---|---|
| Share a new table | Add an `assets` entry | PR → plan → apply |
| Update an analysis notebook | Edit the notebook in the workspace (its source path) | plan shows `UPDATE` → apply |
| Stop sharing a table | Remove the entry | apply with `prune=true` (without prune it stays and shows as `DRIFT`) |
| Give a team run access | Add a `grants` entry with `EXECUTE_CLEAN_ROOM_TASK` | apply |
| Join a room a partner created | Add the room with `create: false` and your alias | apply (manages your side only) |
| New partner, or change partner/region | **New room** with a new name; destroy the old one when done | apply, then destroy the old one |
| Retire a room | Run destroy for that room only | `mode=destroy`, `rooms=<name>`, `confirm_destroy=<name>`; then remove it from the config |

---

## 10. Working alongside Terraform

Keep Terraform for everything it still supports (catalogs, schemas, tables, groups, table grants,
service principals, jobs). Use this tool only for clean room objects.

**Option A (recommended):** separate pipelines. Terraform runs first (data and permissions), then the clean room apply job.

**Option B:** call the tool from Terraform so one `terraform apply` does both:

```hcl
resource "terraform_data" "clean_rooms" {
  triggers_replace = filesha256("${path.module}/config/prod.yaml")

  provisioner "local-exec" {
    command = "python3 cleanroom_iac.py --config config/prod.yaml --mode apply --profile ${var.databricks_profile} --state-path /Workspace/Shared/cleanroom_iac/state/prod.json"
  }

  depends_on = [databricks_grants.cleanroom_owner_access]   # data access first
}
```

Limits of option B: `terraform plan` doesn't show clean room changes, so run `--mode plan` in the
pipeline. Destroying the Terraform resource doesn't delete rooms; use `--mode destroy`.

**When Terraform support returns:** stop the apply and drift jobs, write the Terraform resources,
`terraform import` the existing rooms, assets and grants, and confirm `terraform plan` shows no changes.
Then retire this repo.

---

## 11. Troubleshooting

| Message / symptom | Cause | Fix |
|---|---|---|
| `Clean room owner must be empty or match the caller` | Owner sent on create | Already handled (create, then transfer). If you see it, you're on an old copy of the code |
| `Cloud/region azure/eastus2 is not supported` | Central region not supported | Set `region` to a supported region (section 2) |
| `Request collaborators should include the creator at most one time` | Collaborator uses your own metastore ID without an invite | Add `invite_recipient_email` + `invite_recipient_workspace_id`, or use the partner's ID |
| `User is not authorized to create a clean room` | Missing `CREATE CLEAN ROOM`, or Clean Rooms not enabled for the workspace/account | Check section 3.2; if granted, escalate to the account team |
| Asset `WARN PERMISSION_DENIED` | Owner group lost access to the source object | Restore the grants from section 3.2 |
| Room `WARN ... FAILED` | Provisioning failed | `destroy` that room, fix the cause, apply again |
| `Timed out ... waiting for ... ACTIVE` | Slow provisioning | Raise `wait_timeout_minutes`; re-run apply (it resumes) |
| Room shows `CREATE` although it existed | Room was deleted (status `DELETED`) | Expected: deleted rooms count as absent |
| `ModuleNotFoundError: cleanroom_iac` | Notebook not run from the repo folder | Run the notebook from the Git folder that contains `cleanroom_iac.py` |

---

## 12. Rollback

| Situation | Rollback |
|---|---|
| Bad config merged | Revert the commit and run apply (assets and grants revert; use `prune=true` to remove additions) |
| Wrong asset shared | Remove it and apply with `prune=true`, or delete the asset in the UI immediately |
| Room created wrong (immutable settings) | `destroy` that room; fix the config with a new name; apply |
| Tool misbehaves | Disable both jobs; manage in the UI; the tool never deletes anything without `prune` or `destroy` |

---

## 13. Validation status of this repo (read before go-live)

| Area | Status |
|---|---|
| Engine logic (45 offline tests) | Tested |
| API paths and payloads (SDK 0.169.0 sources) | Checked against the source |
| Plan, drift, destroy guard, state file, run log, serverless job run on Azure | Tested live (internal test workspace) |
| Create request shape (owner, region and same-metastore rules) | Tested live, up to the authorization check |
| Room create → ACTIVE, assets, rules, grants, notebook update, destroy | **Not tested live.** Section 7.1 is the first real test |
