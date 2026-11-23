# Clean Rooms as Code: Pre-setup for the dry run

**Purpose:** what to ask each admin for, and what you then do yourself, to run the smoke test
(guide section 7.1) as a service principal.
**Setup used:** the simple test setup. The service principal is the clean room owner, so no owner group
is needed. Production should use an owner group (guide section 3), and that ownership transfer is
tested separately later.

---

## 1. Summary checklist

| # | Who | What | Done |
|---|---|---|---|
| A1 | Account admin | Confirm Clean Rooms is enabled for the account and test workspace | [ ] |
| A2 | Account admin | Create the service principal and assign it to the test workspace | [ ] |
| A3 | Workspace admin | Confirm serverless compute is enabled for jobs | [ ] |
| A4 | Workspace admin | Give you the **Service Principal User** role on the service principal | [ ] |
| A5 | Metastore admin | Grant `CREATE CLEAN ROOM` and read access on one test table to the service principal | [ ] |
| A6 | Partner (second metastore) | Send their metastore's sharing identifier | [ ] |
| B1 | You | Run the offline tests | [ ] |
| B2 | You | Create the Git folder and give the service principal access to it | [ ] |
| B3 | You | Write `config/dev.yaml` | [ ] |
| B4 | You | Create a job that runs as the service principal | [ ] |
| B5 | You | Run plan, then the smoke test | [ ] |

Values used below; fill these in first:

| Placeholder | What it is | How to find it |
|---|---|---|
| `<workspace-url>` | test workspace URL | browser address bar |
| `<workspace-id>` | numeric workspace ID | the `?o=` number in the workspace URL |
| `<sp-id>` | service principal's numeric ID | output of A2 |
| `<sp-application-id>` | service principal's application ID (UUID) | output of A2 |
| `<metastore-id>` | metastore ID | `databricks metastores summary` |
| `<catalog>.<schema>.<tbl>` | one small, non-sensitive **regular** table | your choice; not a streaming table or materialized view |
| `<your-email>` | your workspace user name | `databricks current-user me` |

---

## 2. Requests for admins (by role)

### Account admin

**A1. Confirm Clean Rooms is enabled** for the account and for `<workspace-url>`.
There is no setting you can check yourself. This is the most likely blocker: a test workspace
can hold every privilege and still return "User is not authorized to create a clean room".

**A2. Create the service principal and assign it to the test workspace**

```bash
databricks account service-principals create --display-name sp-cleanroom-iac-dev
# note "id" (= <sp-id>) and "applicationId" (= <sp-application-id>) from the output

databricks account workspace-assignment update <workspace-id> <sp-id> --json '{"permissions": ["USER"]}'
```

The service principal needs **no admin role**.

### Workspace admin

**A3. Confirm serverless compute is enabled** for jobs (workspace admin settings).

**A4. Give `<your-email>` the Service Principal User role** on `sp-cleanroom-iac-dev`, so you
can set it as a job's "run as".
In the UI: Settings > Identity and access > Service principals > `sp-cleanroom-iac-dev` >
Permissions > Grant access > `<your-email>` > **Service Principal: User**.

No OAuth secret is needed: the test runs as a job, not from your laptop.

### Metastore admin

**A5. Grant the service principal what the test needs** (run in a SQL editor or notebook):

```sql
GRANT CREATE CLEAN ROOM ON METASTORE TO `<sp-application-id>`;
GRANT USE CATALOG ON CATALOG <catalog>                TO `<sp-application-id>`;
GRANT USE SCHEMA  ON SCHEMA  <catalog>.<schema>       TO `<sp-application-id>`;
GRANT SELECT      ON TABLE   <catalog>.<schema>.<tbl> TO `<sp-application-id>`;
```

Check (anyone can run these):

```bash
databricks grants get-effective metastore <metastore-id> --principal <sp-application-id>
databricks grants get-effective table <catalog>.<schema>.<tbl> --principal <sp-application-id>
```

Expected: `CREATE_CLEAN_ROOM` in the first, `SELECT` in the second.

### Partner (or an admin of a second metastore)

**A6. Send the sharing identifier** of their metastore (`cloud:region:metastore-uuid`).
Getting it requires `CREATE CLEAN ROOM` or metastore admin on their side. A clean room always needs a
collaborator on a **different** metastore.

---

## 3. What you do (after section 2 is done)

### B1. Run the offline tests (laptop, no workspace needed)

```bash
git clone <repo-url> cleanroom-iac && cd cleanroom-iac
python3 -m unittest discover -s tests
```

Expected: `OK`.

### B2. Create the Git folder and give the service principal access

1. In the workspace: Workspace > Create > **Git folder** > `<repo-url>`, in your user folder
   (`/Workspace/Users/<your-email>/cleanroom-iac`).
2. Give the service principal read access to it, and create a state folder it can write to:

```bash
# Git folder: CAN_READ for the service principal
databricks workspace get-status /Users/<your-email>/cleanroom-iac -o json      # note "object_id"
databricks workspace update-permissions directories <object_id> --json \
  '{"access_control_list": [{"service_principal_name": "<sp-application-id>", "permission_level": "CAN_READ"}]}'

# State folder: CAN_MANAGE for the service principal
databricks workspace mkdirs /Shared/cleanroom_iac_state
databricks workspace get-status /Shared/cleanroom_iac_state -o json             # note "object_id"
databricks workspace update-permissions directories <object_id> --json \
  '{"access_control_list": [{"service_principal_name": "<sp-application-id>", "permission_level": "CAN_MANAGE"}]}'
```

`update-permissions` adds to the existing permissions; it doesn't replace them.

### B3. Write `config/dev.yaml` in the Git folder

```yaml
version: 1
defaults:
  cloud_vendor: azure            # your cloud: aws | azure | gcp
  region: eastus                 # a region Clean Rooms supports (can differ from the metastore's)
clean_rooms:
  - name: zz_cr_iac_smoke
    comment: "Smoke test - safe to delete"
    local_alias: acme
    collaborators:
      - alias: partner
        global_metastore_id: "<sharing identifier from A6>"
    assets:
      - type: TABLE
        local_name: "<catalog>.<schema>.<tbl>"
```

Don't set `owner`, on the room or in `defaults`: the service principal stays the owner.

### B4. Create a job that runs as the service principal

Save as `job.json`:

```json
{
  "name": "cleanroom-iac-smoke-test",
  "max_concurrent_runs": 1,
  "run_as": {"service_principal_name": "<sp-application-id>"},
  "tasks": [{
    "task_key": "run",
    "notebook_task": {
      "notebook_path": "/Workspace/Users/<your-email>/cleanroom-iac/cleanroom_iac_runner",
      "base_parameters": {
        "config_path": "config/dev.yaml",
        "mode": "plan",
        "state_path": "/Workspace/Shared/cleanroom_iac_state/dev.json"
      }
    }
  }]
}
```

```bash
databricks jobs create --json @job.json        # note "job_id"
```

No cluster is specified, so it runs on serverless.

### B5. Run plan, then the smoke test

Each run overrides only the parameters it changes:

```bash
# T3: plan (read-only) -- check the output for PREFLIGHT warnings first
databricks jobs run-now <job_id> --json '{"notebook_params": {"mode": "plan"}}'

# T4: apply (creates the room), then T5: apply again (must change nothing)
databricks jobs run-now <job_id> --json '{"notebook_params": {"mode": "apply"}}'
databricks jobs run-now <job_id> --json '{"notebook_params": {"mode": "apply"}}'

# T6: drift check (edit the room comment in the UI first; this run must FAIL with "Drift detected")
databricks jobs run-now <job_id> --json '{"notebook_params": {"mode": "drift", "fail_on_drift": "true"}}'

# T12: destroy (wrong confirmation is refused; the exact room name deletes it)
databricks jobs run-now <job_id> --json '{"notebook_params": {"mode": "destroy", "confirm_destroy": "yes"}}'
databricks jobs run-now <job_id> --json '{"notebook_params": {"mode": "destroy", "confirm_destroy": "zz_cr_iac_smoke"}}'
```

Read each result in the Jobs UI (run > output table), and tick the matching step in guide section 7.1.
For this setup, T4's expected owner is **the service principal**, not a group.

---

## 4. If plan shows a PREFLIGHT warning

| Warning mentions | Missing step |
|---|---|
| `CREATE_CLEAN_ROOM ... was not found` | A5 (first GRANT) |
| `source ... was not found` | A5 (table grants), or a typo in `local_name` |
| `Delta Sharing scope` | metastore admin: enable Delta Sharing |
| `could not check` | the service principal can't read that API; send the output to whoever supports the tool |

If plan is clean but the first apply says **"User is not authorized to create a clean room"**, go back to
A1 (Clean Rooms enablement).

---

## 5. Message to send to the admins

> Hi team, to test our Clean Rooms automation in `<workspace-url>` I need:
>
> **Account admin**
> 1. Confirm Clean Rooms is enabled for our account and this workspace.
> 2. Create service principal `sp-cleanroom-iac-dev` and assign it to this workspace (USER, no admin role).
>
> **Workspace admin**
> 3. Confirm serverless compute is enabled for jobs.
> 4. Give me (`<your-email>`) the *Service Principal: User* role on `sp-cleanroom-iac-dev`.
>
> **Metastore admin**
> 5. Run:
>    ```sql
>    GRANT CREATE CLEAN ROOM ON METASTORE TO `<sp-application-id>`;
>    GRANT USE CATALOG ON CATALOG <catalog> TO `<sp-application-id>`;
>    GRANT USE SCHEMA ON SCHEMA <catalog>.<schema> TO `<sp-application-id>`;
>    GRANT SELECT ON TABLE <catalog>.<schema>.<tbl> TO `<sp-application-id>`;
>    ```
>
> The service principal needs no admin roles. Thanks!
