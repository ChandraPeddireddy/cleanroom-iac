# Databricks notebook source
# MAGIC %md
# MAGIC # Clean Rooms as code: runner
# MAGIC
# MAGIC Interim replacement for the Terraform clean room resources removed in provider v1.92.0.
# MAGIC Reads a YAML/JSON config from Git and converges Clean Rooms, assets, auto-approval rules
# MAGIC and `CLEAN_ROOM` grants through the REST API. Safe to re-run.
# MAGIC
# MAGIC | mode | effect |
# MAGIC |---|---|
# MAGIC | `plan` (default) | read-only, shows what `apply` would change |
# MAGIC | `apply` | creates or updates to match the config |
# MAGIC | `drift` | read-only; with `fail_on_drift=true` the run fails when live state differs (for scheduled alerting) |
# MAGIC | `destroy` | deletes the selected rooms; `confirm_destroy` must equal the comma-separated, sorted room names |
# MAGIC
# MAGIC **Run as:** a group-owned service principal with `CREATE_CLEAN_ROOM` on the metastore (or metastore admin),
# MAGIC ownership or the needed privileges on every shared object, and `MANAGE` on the room for grants.
# MAGIC Run it as a job with `max_concurrent_runs = 1`.

# COMMAND ----------

# Clear leftover widgets on interactive runs, for example after switching from a SQL notebook.
# Skipped inside jobs, so job parameters are never discarded.
import json

def _running_in_job() -> bool:
    try:
        ctx = json.loads(dbutils.notebook.entry_point.getDbutils().notebook().getContext().safeToJson())
        return bool(ctx.get("attributes", {}).get("jobId"))
    except Exception:
        return True  # unknown context: be conservative and keep widgets

IN_JOB = _running_in_job()
if not IN_JOB:
    dbutils.widgets.removeAll()
print(f"Running in job: {IN_JOB} | widgets reset: {not IN_JOB}")

# COMMAND ----------

dbutils.widgets.text("config_path", "config/cleanrooms.yaml", "1. Config file (relative to this notebook or absolute)")
dbutils.widgets.dropdown("mode", "plan", ["plan", "apply", "drift", "destroy"], "2. Mode")
dbutils.widgets.text("rooms", "", "3. Rooms (comma-separated, blank = all)")
dbutils.widgets.dropdown("prune", "false", ["false", "true"], "4. Prune unmanaged assets/rules/grants")
dbutils.widgets.dropdown("allow_replace", "false", ["false", "true"], "5. Allow asset replace")
dbutils.widgets.text("state_path", "", "6. State file (workspace path, enables notebook change detection)")
dbutils.widgets.dropdown("fail_on_drift", "false", ["false", "true"], "7. Fail run on drift")
dbutils.widgets.text("confirm_destroy", "", "8. Confirm destroy (sorted room names)")
dbutils.widgets.text("log_table", "", "9. Run log Delta table (catalog.schema.table, optional)")

# COMMAND ----------

import os
import re
import sys
import uuid
from datetime import datetime, timezone

# The engine module lives next to this notebook in the same Git folder.
NOTEBOOK_DIR = os.getcwd()
if NOTEBOOK_DIR not in sys.path:
    sys.path.insert(0, NOTEBOOK_DIR)

import importlib
import cleanroom_iac as cr
importlib.reload(cr)

from databricks.sdk import WorkspaceClient

def _flag(name: str) -> bool:
    return dbutils.widgets.get(name).strip().lower() == "true"

MODE = dbutils.widgets.get("mode").strip().lower()
CONFIG_PATH = dbutils.widgets.get("config_path").strip()
ROOMS = [r.strip() for r in dbutils.widgets.get("rooms").split(",") if r.strip()] or None
PRUNE = _flag("prune")
ALLOW_REPLACE = _flag("allow_replace")
STATE_PATH = dbutils.widgets.get("state_path").strip() or None
FAIL_ON_DRIFT = _flag("fail_on_drift")
CONFIRM_DESTROY = dbutils.widgets.get("confirm_destroy").strip()
LOG_TABLE = dbutils.widgets.get("log_table").strip()

if MODE not in {"plan", "apply", "drift", "destroy"}:
    raise ValueError(f"Unsupported mode: {MODE}")
if LOG_TABLE and not re.fullmatch(r"[\w`-]+\.[\w`-]+\.[\w`-]+", LOG_TABLE):
    raise ValueError("log_table must be catalog.schema.table")

if not os.path.isabs(CONFIG_PATH):
    CONFIG_PATH = os.path.join(NOTEBOOK_DIR, CONFIG_PATH)

rooms_cfg, warnings = cr.normalize_config(cr.load_config_file(CONFIG_PATH))
for w_msg in warnings:
    print("WARNING:", w_msg)

w = WorkspaceClient()
do = cr.sdk_transport(w)
mgr = cr.CleanRoomManager(do, rooms_cfg, state=cr.WorkspaceFileState(do, STATE_PATH))

ms = mgr.metastore
print(f"Engine {cr.__version__} | mode={MODE} | config={CONFIG_PATH}")
print(f"Metastore {ms.get('name')} ({ms.get('global_metastore_id')}) | run as {w.current_user.me().user_name}")
print(f"Rooms: {[r['name'] for r in rooms_cfg if not ROOMS or r['name'] in ROOMS]} | prune={PRUNE} "
      f"allow_replace={ALLOW_REPLACE} state={STATE_PATH or 'none'}")

# COMMAND ----------

if MODE in ("plan", "drift"):
    changes = mgr.plan(ROOMS, prune=PRUNE, allow_replace=ALLOW_REPLACE)
elif MODE == "apply":
    changes = mgr.apply(ROOMS, prune=PRUNE, allow_replace=ALLOW_REPLACE)
else:
    changes = mgr.destroy(ROOMS, confirm=CONFIRM_DESTROY)

COLUMNS = ["room", "resource_type", "resource", "action", "status", "detail", "error"]
rows = [{k: str(c.as_row().get(k) or "") for k in COLUMNS} for c in changes]
result_df = spark.createDataFrame(rows, "room string, resource_type string, resource string, action string, "
                                        "status string, detail string, error string")
display(result_df)

# COMMAND ----------

if LOG_TABLE:
    run_id = str(uuid.uuid4())
    run_ts = datetime.now(timezone.utc).isoformat()
    from pyspark.sql import functions as F
    (result_df
        .withColumn("run_id", F.lit(run_id))
        .withColumn("run_ts", F.lit(run_ts).cast("timestamp"))
        .withColumn("mode", F.lit(MODE))
        .withColumn("run_as", F.lit(w.current_user.me().user_name))
        .withColumn("metastore", F.lit(ms.get("global_metastore_id")))
        .write.mode("append").option("mergeSchema", "true").saveAsTable(LOG_TABLE))
    print(f"Logged {len(rows)} rows to {LOG_TABLE} (run_id={run_id})")

failed = [c for c in changes if c.status == "FAILED"]
pending = [c for c in changes if c.action in cr.MUTATING_ACTIONS and c.status == "PLANNED"]
drifted = [c for c in changes if c.action == cr.DRIFT]
print(f"Summary: {len(changes)} items | pending changes={len(pending)} | drift={len(drifted)} | failed={len(failed)}")

if failed:
    raise RuntimeError(f"{len(failed)} change(s) failed; see the table above:\n" +
                       "\n".join(f"  {c.room} {c.resource_type} {c.resource}: {c.error}" for c in failed))
if MODE == "drift" and FAIL_ON_DRIFT and cr.has_drift(changes):
    raise RuntimeError(f"Drift detected: {len(pending)} pending change(s), {len(drifted)} drift item(s)")
