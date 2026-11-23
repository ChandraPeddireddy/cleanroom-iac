"""
cleanroom_iac -- configuration-driven, idempotent management of Databricks Clean Rooms.

Interim replacement for the Terraform resources removed in provider v1.92.0
(databricks_clean_rooms_clean_room, databricks_clean_room_asset,
databricks_clean_room_auto_approval_rule) plus Unity Catalog grants on the
CLEAN_ROOM securable (MODIFY_CLEAN_ROOM, EXECUTE_CLEAN_ROOM_TASK).

Modes
  plan     read-only; shows what apply would change (like `terraform plan`)
  apply    converges live state to the config; safe to re-run
  drift    read-only; same as plan, intended for scheduled jobs that alert on drift
  destroy  deletes the clean rooms named in the config (explicit confirmation required)

The engine talks to Databricks only through a transport callable
    do(method, path, query=None, body=None) -> dict
so the same code runs from a notebook / job (Databricks SDK transport) or from a
CI runner / Terraform local-exec (Databricks CLI transport). No tokens are handled here.

REST endpoints used (all verified against databricks-sdk-py 0.169.0 sources):
  GET    /api/2.1/unity-catalog/metastore_summary
  POST   /api/2.0/clean-rooms                              (body = CleanRoom)
  GET    /api/2.0/clean-rooms/{name}
  PATCH  /api/2.0/clean-rooms/{name}                       (body = {"clean_room": {...}})
  DELETE /api/2.0/clean-rooms/{name}
  POST   /api/2.0/clean-rooms/{name}/output-catalogs       (body = {"catalog_name": ...})
  GET    /api/2.0/clean-rooms/{name}/assets
  GET    /api/2.0/clean-rooms/{name}/assets/{type}/{asset}
  POST   /api/2.0/clean-rooms/{name}/assets                (body = CleanRoomAsset)
  PATCH  /api/2.0/clean-rooms/{name}/assets/{type}/{asset} (body = CleanRoomAsset)
  DELETE /api/2.0/clean-rooms/{name}/assets/{type}/{asset}
  GET    /api/2.0/clean-rooms/{name}/auto-approval-rules
  POST   /api/2.0/clean-rooms/{name}/auto-approval-rules   (body = {"auto_approval_rule": {...}})
  DELETE /api/2.0/clean-rooms/{name}/auto-approval-rules/{rule_id}
  GET    /api/2.1/unity-catalog/permissions/CLEAN_ROOM/{name}
  PATCH  /api/2.1/unity-catalog/permissions/CLEAN_ROOM/{name}
  GET    /api/2.0/workspace/export ; POST /api/2.0/workspace/import ; POST /api/2.0/workspace/mkdirs
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import posixpath
import re
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional, Tuple

__version__ = "0.1.0"

CLEAN_ROOMS_API = "/api/2.0/clean-rooms"
UC_PERMISSIONS_API = "/api/2.1/unity-catalog/permissions/CLEAN_ROOM"
METASTORE_SUMMARY_API = "/api/2.1/unity-catalog/metastore_summary"

# Asset types whose payload is just a Unity Catalog local name.
LOCAL_DETAILS_FIELD = {
    "TABLE": "table_local_details",
    "VIEW": "view_local_details",
    "VOLUME": "volume_local_details",
    "FOREIGN_TABLE": "foreign_table_local_details",
}
NOTEBOOK = "NOTEBOOK_FILE"
SUPPORTED_ASSET_TYPES = set(LOCAL_DETAILS_FIELD) | {NOTEBOOK}

# Privileges documented for the CLEAN_ROOM securable. Others are passed through
# with a warning so the API, not this tool, is the final authority.
KNOWN_CLEAN_ROOM_PRIVILEGES = {"MODIFY_CLEAN_ROOM", "EXECUTE_CLEAN_ROOM_TASK"}

CLOUD_VENDORS = ("aws", "azure", "gcp")
GLOBAL_METASTORE_ID_RE = re.compile(r"^(aws|azure|gcp):[a-z0-9-]+:[0-9a-f-]{36}$")
REGION_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")   # e.g. eastus, us-west-2, europe-west1

SECURABLE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,255}$")
THREE_PART_RE = re.compile(r"^[^.\s]+\.[^.\s]+\.[^.\s]+$")

# Pre-flight: source object types per declared asset type. Types in UNVERIFIED_SOURCE_TYPES can exist
# but sharing them into a clean room has not been verified with this tool.
EXPECTED_SOURCE_TYPES = {
    "TABLE": {"MANAGED", "EXTERNAL", "MANAGED_SHALLOW_CLONE", "EXTERNAL_SHALLOW_CLONE"},
    "VIEW": {"VIEW"},
    "FOREIGN_TABLE": {"FOREIGN"},
}
UNVERIFIED_SOURCE_TYPES = {"STREAMING_TABLE", "MATERIALIZED_VIEW", "METRIC_VIEW"}
PREFLIGHT = "PREFLIGHT"
ALL_ROOMS = "(all rooms)"

# Change actions
CREATE, UPDATE, DELETE, REPLACE = "CREATE", "UPDATE", "DELETE", "REPLACE"
NOOP, DRIFT, WARN, WAIT = "NOOP", "DRIFT", "WARN", "WAIT"
MUTATING_ACTIONS = {CREATE, UPDATE, DELETE, REPLACE}


def _clean_setting(value: Any) -> Tuple[Optional[str], Optional[str]]:
    """Normalize one string setting. Returns (value, error): empty -> (None, None); non-string YAML
    values such as true, 123 or lists -> (None, error); otherwise the trimmed, lower-cased string."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, None
    if not isinstance(value, str):
        return None, f"must be a string, got {type(value).__name__} {value!r}"
    return value.strip().lower(), None


def _value_error(key: str, value: str) -> Optional[str]:
    """Format check for a normalized cloud_vendor / region value (support is decided by the API)."""
    if key == "cloud_vendor" and value not in CLOUD_VENDORS:
        return f"must be one of {list(CLOUD_VENDORS)} (got {value!r})"
    if key == "region" and not REGION_RE.match(value):
        return f"is not a valid region identifier (got {value!r})"
    return None


def _int_value(value: Any, what: str, positive: bool = False) -> Tuple[Optional[int], Optional[str]]:
    """Parse an optional whole-number setting: an int, a whole float (45.0) or an ASCII digit string
    ("45"). Returns (value, error); empty -> (None, None). positive=True also requires > 0."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, None
    bad = f"{what} must be a whole number (got {value!r})"
    if isinstance(value, bool):
        return None, bad
    if isinstance(value, float):
        if not value.is_integer():
            return None, bad
        number = int(value)
    elif isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        number = int(value.strip())
    else:
        return None, bad
    if positive and number <= 0:
        return None, f"{what} must be greater than 0 (got {value!r})"
    return number, None


def _entries(room: dict, key: str, where: str, errors: List[str]) -> List[Any]:
    """The room's list setting (collaborators, assets, ...). A missing/null value is an empty list;
    any other non-list value is reported as an error."""
    value = room.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(f"{where}: '{key}' must be a list (got {type(value).__name__})")
        return []
    return value


def _setting(room: dict, clean_defaults: Dict[str, Optional[str]], key: str) -> Tuple[Optional[str], Optional[str]]:
    """Room value if set and non-empty, else the (already validated) defaults value.
    Returns (value, error); the error concerns the room's own value only."""
    value, err = _clean_setting(room.get(key))
    if err:
        return None, f"'{key}' {err}"
    if value is not None:
        return value, None
    return clean_defaults.get(key), None


# --------------------------------------------------------------------------- #
# Errors and transports
# --------------------------------------------------------------------------- #
class ApiError(Exception):
    def __init__(self, message: str, error_code: Optional[str] = None, not_found: bool = False):
        super().__init__(message)
        self.error_code = error_code
        self.not_found = not_found


class ConfigError(ValueError):
    pass


Transport = Callable[..., Any]

_NOT_FOUND_HINTS = re.compile(r"does not exist|doesn't exist|not found|RESOURCE_DOES_NOT_EXIST|NOT_FOUND", re.I)


def sdk_transport(workspace_client) -> Transport:
    """Transport backed by databricks-sdk WorkspaceClient (notebooks, jobs)."""
    from databricks.sdk.errors import DatabricksError, NotFound  # lazy import

    def do(method: str, path: str, query: Optional[dict] = None, body: Optional[dict] = None):
        try:
            res = workspace_client.api_client.do(method, path, query=query, body=body)
            return res if res is not None else {}
        except NotFound as e:
            raise ApiError(str(e), getattr(e, "error_code", None), not_found=True) from e
        except DatabricksError as e:
            msg = str(e)
            raise ApiError(msg, getattr(e, "error_code", None),
                           not_found=bool(_NOT_FOUND_HINTS.search(msg))) from e

    return do


def cli_transport(profile: Optional[str] = None, cli: str = "databricks") -> Transport:
    """Transport backed by the Databricks CLI `databricks api` command (CI, local-exec).
    Authentication is whatever the CLI profile already uses; no token is read here."""

    def do(method: str, path: str, query: Optional[dict] = None, body: Optional[dict] = None):
        full_path = path
        if query:
            full_path += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        cmd = [cli, "api", method.lower(), full_path, "-o", "json"]
        if body is not None:
            cmd += ["--json", json.dumps(body)]
        if profile:
            cmd += ["-p", profile]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout).strip()
            raise ApiError(msg, not_found=bool(_NOT_FOUND_HINTS.search(msg)))
        out = proc.stdout.strip()
        return json.loads(out) if out else {}

    return do


# --------------------------------------------------------------------------- #
# Config loading and validation
# --------------------------------------------------------------------------- #
def load_config_text(text: str, fmt: str) -> dict:
    if fmt == "json":
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise ConfigError(f"Config is not valid JSON: {e}") from e
    try:
        import yaml  # PyYAML ships with Databricks Runtime
    except ImportError as e:  # pragma: no cover
        raise ConfigError("PyYAML not available; use a .json config or %pip install pyyaml") from e
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"Config is not valid YAML: {e}") from e


def load_config_file(path: str) -> dict:
    fmt = "json" if path.lower().endswith(".json") else "yaml"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as e:
        raise ConfigError(f"Cannot read config file {path}: {e}") from e
    return load_config_text(text, fmt)


ROOM_TEXT_FIELDS = ("name", "comment", "owner", "local_alias", "output_catalog")
DEFAULT_TEXT_FIELDS = ("owner",)
LIST_SECTIONS = {  # room list setting -> text fields of each entry
    "collaborators": ("alias", "global_metastore_id", "invite_recipient_email"),
    "assets": ("type", "name", "local_name", "workspace_path", "description"),
    "auto_approval_rules": ("author_alias", "author_scope", "runner_alias"),
    "grants": ("principal",),
}


def _type_errors(raw: dict) -> List[str]:
    """Check scalar value TYPES for the whole config in one pass, before any other validation logic
    runs, so a wrong-typed value is reported instead of crashing. Shape problems (a section that is not
    a list, an entry that is not a mapping), numbers, create, cloud_vendor and region are checked by the
    main validation, which lists them together with all other errors."""
    errs: List[str] = []

    def text(d: dict, keys, where: str) -> None:
        for k in keys:
            v = d.get(k)
            if v is not None and not isinstance(v, str):
                hint = "; put it in quotes" if isinstance(v, (bool, int, float)) else ""
                errs.append(f"{where}: '{k}' must be text (got {type(v).__name__} {v!r}){hint}")

    def text_list(v: Any, where: str, k: str) -> None:
        if v is None:
            return
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            errs.append(f"{where}: '{k}' must be a list of text values (got {v!r})")

    defaults = raw.get("defaults")
    if defaults is not None and not isinstance(defaults, dict):
        errs.append(f"'defaults' must be a mapping (got {type(defaults).__name__})")
    elif defaults:
        text(defaults, DEFAULT_TEXT_FIELDS, "defaults")
    rooms = raw.get("clean_rooms")
    if not isinstance(rooms, list):
        return errs  # reported by the structural check
    for i, r in enumerate(rooms):
        if not isinstance(r, dict):
            continue  # reported later as "must be a mapping"
        name = r.get("name")
        where = f"clean room '{name}'" if isinstance(name, str) and name else f"clean room '{i}'"
        text(r, ROOM_TEXT_FIELDS, where)
        shared_output = r.get("enable_shared_output")
        if shared_output is not None and not isinstance(shared_output, bool):
            # a quoted "false" would otherwise be sent as true
            errs.append(f"{where}: 'enable_shared_output' must be true or false without quotes "
                        f"(got {shared_output!r})")
        egress = r.get("egress_network_policy")
        if egress is not None and not isinstance(egress, dict):
            errs.append(f"{where}: 'egress_network_policy' must be a mapping (got {type(egress).__name__})")
        for section, fields in LIST_SECTIONS.items():
            items = r.get(section)
            if items is None:
                continue
            if not isinstance(items, list):
                continue  # shape problem: reported by the main validation, together with other errors
            for j, item in enumerate(items):
                if not isinstance(item, dict):
                    continue  # null entries / shape problems are handled by the main validation
                at = f"{where}: {section}[{j}]"
                text(item, fields, at)
                if section == "assets":
                    text_list(item.get("runner_aliases"), at, "runner_aliases")
                    env = item.get("environment_version")
                    if env is not None and (isinstance(env, bool) or not isinstance(env, (str, int, float))):
                        errs.append(f"{at}: 'environment_version' must be text or a number (got {env!r})")
                    parts = item.get("partitions")
                    if parts is not None and not isinstance(parts, list):
                        errs.append(f"{at}: 'partitions' must be a list (got {type(parts).__name__})")
                if section == "grants":
                    privs = item.get("privileges")
                    if privs and isinstance(privs, list):  # a non-list is reported by the main validation
                        text_list(privs, at, "privileges")
    return errs


def normalize_config(raw: dict) -> Tuple[List[dict], List[str]]:
    """Validate the config and return (rooms, warnings). Raises ConfigError listing every problem.
    Wrong value types are reported first (all of them); a malformed config never raises anything
    other than ConfigError."""
    if not isinstance(raw, dict):
        raise ConfigError("Config root must be a mapping")
    type_errors = _type_errors(raw)
    if type_errors:
        raise ConfigError("Invalid clean room config (wrong value types; fix these first):\n  - "
                          + "\n  - ".join(type_errors))
    try:
        return _normalize_config(raw)
    except ConfigError:
        raise
    except (TypeError, AttributeError, ValueError, KeyError) as e:  # safety net: never a traceback
        raise ConfigError(f"Invalid clean room config: unexpected value ({type(e).__name__}: {e}). "
                          f"Compare the field types with config/cleanrooms.example.yaml") from e


def _normalize_config(raw: dict) -> Tuple[List[dict], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    if not isinstance(raw, dict):
        raise ConfigError("Config root must be a mapping")
    defaults = raw.get("defaults") or {}
    rooms_raw = raw.get("clean_rooms") or []
    if not isinstance(rooms_raw, list) or not rooms_raw:
        raise ConfigError("Config must contain a non-empty 'clean_rooms' list")

    # cloud_vendor / region defaults are validated once. A bad default is reported once, and only if a
    # room the tool creates actually inherits it (join-only rooms never use these settings).
    clean_defaults: Dict[str, Optional[str]] = {}
    bad_defaults: Dict[str, str] = {}
    used_bad_defaults = set()
    joined_bad_defaults = set()  # bad defaults inherited only by join-only rooms -> warning, not error
    for key in ("cloud_vendor", "region"):
        value, err = _clean_setting(defaults.get(key))
        if not err and value is not None:
            err = _value_error(key, value)
        clean_defaults[key] = None if err else value
        if err:
            bad_defaults[key] = f"defaults: '{key}' {err}"

    default_timeout, default_timeout_err = _int_value(defaults.get("wait_timeout_minutes"),
                                                      "defaults: 'wait_timeout_minutes'", positive=True)
    bad_default_timeout_used = False

    seen_rooms = set()
    rooms: List[dict] = []
    for i, r in enumerate(rooms_raw):
        where = f"clean_rooms[{i}]"
        if not isinstance(r, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        name = r.get("name")
        where = f"clean room '{name or i}'"
        if not name or not SECURABLE_NAME_RE.match(str(name)):
            errors.append(f"{where}: 'name' is required and may contain only letters, digits, '_' and '-'")
            continue
        if name in seen_rooms:
            errors.append(f"{where}: duplicate clean room name")
        seen_rooms.add(name)

        create_raw = r.get("create", True)
        if isinstance(create_raw, bool):
            manage_room = create_raw
        else:
            # A quoted "false" would otherwise count as true and CREATE a room meant to be joined.
            errors.append(f"{where}: 'create' must be true or false without quotes (got {create_raw!r})")
            manage_room = False
        local_alias = r.get("local_alias")
        if not local_alias or not SECURABLE_NAME_RE.match(str(local_alias)):
            errors.append(f"{where}: 'local_alias' (this metastore's collaborator alias) is required")

        collabs = []
        aliases = {local_alias} if local_alias else set()
        for j, c in enumerate(_entries(r, "collaborators", where, errors)):
            c = {} if c is None else c
            if not isinstance(c, dict):
                errors.append(f"{where}: collaborators[{j}] must be a mapping (got {c!r})")
                continue
            alias = c.get("alias")
            gmid = c.get("global_metastore_id")
            if not alias or not SECURABLE_NAME_RE.match(str(alias)):
                errors.append(f"{where}: collaborators[{j}].alias is required")
                continue
            if alias in aliases:
                errors.append(f"{where}: collaborator alias '{alias}' is not unique")
            aliases.add(alias)
            if (gmid or manage_room) and not GLOBAL_METASTORE_ID_RE.match(str(gmid or "")):
                errors.append(f"{where}: collaborator '{alias}' global_metastore_id must look like "
                              f"cloud:region:metastore-uuid (got {gmid!r})")
            email = c.get("invite_recipient_email")
            ws_id, ws_err = _int_value(c.get("invite_recipient_workspace_id"),
                                       f"collaborator '{alias}' invite_recipient_workspace_id", positive=True)
            if ws_err:
                errors.append(f"{where}: {ws_err}")
            elif email and not ws_id:
                errors.append(f"{where}: collaborator '{alias}' sets invite_recipient_email, so "
                              f"invite_recipient_workspace_id is required too")
            collabs.append({"alias": alias, "global_metastore_id": gmid,
                            "invite_recipient_email": email,
                            "invite_recipient_workspace_id": ws_id})
        if manage_room and not collabs:
            errors.append(f"{where}: at least one other collaborator is required to create a clean room")
        # Join-only rooms may omit collaborators; alias references cannot be checked locally then.
        check_aliases = bool(collabs) or manage_room
        if not check_aliases:
            warnings.append(f"{where}: no collaborators listed, so runner/author aliases are not validated "
                            f"locally (the API will validate them)")

        assets = []
        seen_assets = set()
        for j, a in enumerate(_entries(r, "assets", where, errors)):
            a = {} if a is None else a
            if not isinstance(a, dict):
                errors.append(f"{where}: assets[{j}] must be a mapping (got {a!r})")
                continue
            atype = str(a.get("type", "")).upper()
            aw = f"{where}: assets[{j}]"
            if atype not in SUPPORTED_ASSET_TYPES:
                errors.append(f"{aw}: type must be one of {sorted(SUPPORTED_ASSET_TYPES)}")
                continue
            if atype == NOTEBOOK:
                nb_name = a.get("name")
                path = a.get("workspace_path")
                if not nb_name or not path:
                    errors.append(f"{aw}: NOTEBOOK_FILE needs 'name' and 'workspace_path'")
                    continue
                runners = list(a.get("runner_aliases") or [])
                for ra in runners:
                    if check_aliases and ra not in aliases:
                        errors.append(f"{aw}: runner alias '{ra}' is not a collaborator of this room")
                asset = {"type": atype, "name": nb_name, "workspace_path": path,
                         "runner_aliases": sorted(runners),
                         "description": a.get("description"),
                         "environment_version": str(a["environment_version"]) if a.get("environment_version") else None}
            else:
                local_name = a.get("local_name")
                if not local_name or not THREE_PART_RE.match(str(local_name)):
                    errors.append(f"{aw}: {atype} needs 'local_name' as catalog.schema.object")
                    continue
                shared = a.get("name")
                if not shared:
                    _, schema, obj = local_name.split(".")
                    shared = f"{local_alias}.{schema}.{obj}"
                elif not THREE_PART_RE.match(str(shared)):
                    errors.append(f"{aw}: 'name' must be shared_catalog.shared_schema.asset_name")
                    continue
                asset = {"type": atype, "name": shared, "local_name": local_name,
                         "partitions": a.get("partitions")}
            key = (asset["type"], asset["name"])
            if key in seen_assets:
                errors.append(f"{aw}: duplicate asset {key}")
            seen_assets.add(key)
            assets.append(asset)

        rules = []
        for j, ru in enumerate(_entries(r, "auto_approval_rules", where, errors)):
            ru = {} if ru is None else ru
            if not isinstance(ru, dict):
                errors.append(f"{where}: auto_approval_rules[{j}] must be a mapping (got {ru!r})")
                continue
            author = ru.get("author_alias")
            scope = ru.get("author_scope")
            runner = ru.get("runner_alias")
            rw = f"{where}: auto_approval_rules[{j}]"
            if bool(author) == bool(scope):
                errors.append(f"{rw}: set exactly one of author_alias or author_scope (ANY_AUTHOR)")
                continue
            if scope and scope != "ANY_AUTHOR":
                errors.append(f"{rw}: author_scope supports only ANY_AUTHOR")
            if check_aliases and author and author not in aliases:
                errors.append(f"{rw}: author alias '{author}' is not a collaborator of this room")
            if not runner or (check_aliases and runner not in aliases):
                errors.append(f"{rw}: runner_alias must be a collaborator of this room")
            rules.append({"author_alias": author, "author_scope": scope, "runner_alias": runner})

        grants: Dict[str, List[str]] = {}
        for j, g in enumerate(_entries(r, "grants", where, errors)):
            g = {} if g is None else g
            if not isinstance(g, dict):
                errors.append(f"{where}: grants[{j}] must be a mapping (got {g!r})")
                continue
            principal = g.get("principal")
            raw_privs = g.get("privileges") or []
            if not isinstance(raw_privs, list):
                # a bare string would otherwise be split into single characters
                errors.append(f"{where}: grants[{j}].privileges must be a list (got {raw_privs!r})")
                continue
            privs = [str(p).upper().replace(" ", "_") for p in raw_privs]
            if not principal or not privs:
                errors.append(f"{where}: grants[{j}] needs 'principal' and a non-empty 'privileges' list")
                continue
            unknown = set(privs) - KNOWN_CLEAN_ROOM_PRIVILEGES
            if unknown:
                warnings.append(f"{where}: grant to '{principal}' uses privileges {sorted(unknown)} that are "
                                f"not documented for clean rooms; the API will decide")
            grants.setdefault(principal, [])
            grants[principal] = sorted(set(grants[principal]) | set(privs))

        owner = r.get("owner", defaults.get("owner"))
        if owner and "@" in str(owner):
            warnings.append(f"{where}: owner '{owner}' is a user. Databricks recommends a group as clean "
                            f"room owner, because asset access depends on the owner's privileges")

        # Cloud and region of the central clean room come ONLY from config (room or defaults);
        # nothing is derived from the workspace metastore. Supported regions are decided by the API.
        cloud_vendor, cloud_err = _setting(r, clean_defaults, "cloud_vendor")
        region, region_err = _setting(r, clean_defaults, "region")
        if not manage_room:
            # Not used for join-only rooms, but surface typos before the room is ever switched to create: true.
            invalid = set()
            for key, value, err in (("cloud_vendor", cloud_vendor, cloud_err), ("region", region, region_err)):
                own_value = _clean_setting(r.get(key))[0]
                if err:
                    warnings.append(f"{where}: {err} (ignored because create: false)")
                    invalid.add(key)
                elif own_value is not None and _value_error(key, own_value):
                    warnings.append(f"{where}: '{key}' {_value_error(key, own_value)} "
                                    f"(ignored because create: false)")
                    invalid.add(key)
                elif own_value is None and key in bad_defaults:
                    joined_bad_defaults.add(key)
            # an invalid value is not kept (same as an invalid default)
            cloud_vendor = None if "cloud_vendor" in invalid else cloud_vendor
            region = None if "region" in invalid else region
        if manage_room:
            skip = set()
            for key, value, err in (("cloud_vendor", cloud_vendor, cloud_err), ("region", region, region_err)):
                if err:
                    errors.append(f"{where}: {err}")
                    skip.add(key)
                elif value is None and key in bad_defaults:
                    used_bad_defaults.add(key)  # reported once after the loop
                    skip.add(key)
            if "cloud_vendor" not in skip and cloud_vendor not in CLOUD_VENDORS:
                errors.append(f"{where}: 'cloud_vendor' must be one of {list(CLOUD_VENDORS)} "
                              f"(set it on the room or in defaults; got {cloud_vendor!r})")
            if "region" not in skip and (not region or not REGION_RE.match(region)):
                errors.append(f"{where}: 'region' of the central clean room is required (set it on the room "
                              f"or in defaults). Use a region Clean Rooms supports for that cloud; it can "
                              f"differ from your metastore's region (got {region!r})")

        wait_timeout, timeout_err = _int_value(r.get("wait_timeout_minutes"), "'wait_timeout_minutes'",
                                               positive=True)
        if timeout_err:
            errors.append(f"{where}: {timeout_err}")
            wait_timeout = 30
        elif wait_timeout is None:  # not set (or empty) on the room: inherit from defaults
            if default_timeout_err:
                bad_default_timeout_used = True
            wait_timeout = default_timeout or 30

        egress = r.get("egress_network_policy")
        rooms.append({
            "name": name,
            "create": manage_room,
            "comment": r.get("comment"),
            "owner": owner,
            "local_alias": local_alias,
            "cloud_vendor": cloud_vendor,
            "region": region,
            "collaborators": collabs,
            "enable_shared_output": r.get("enable_shared_output"),
            "egress_network_policy": egress,
            "output_catalog": r.get("output_catalog"),
            "assets": assets,
            "auto_approval_rules": rules,
            "grants": grants,
            "wait_timeout_minutes": wait_timeout,
        })

    for key in sorted(set(bad_defaults) - used_bad_defaults - joined_bad_defaults):
        warnings.append(f"{bad_defaults[key]} (no room inherits it right now; fix it before a room relies on it)")
    for key in sorted(joined_bad_defaults - used_bad_defaults):
        warnings.append(f"{bad_defaults[key]} (only inherited by create: false rooms, where it is ignored; "
                        f"fix it before any room is switched to create: true)")
    if default_timeout_err and not bad_default_timeout_used:
        warnings.append(f"{default_timeout_err} (no room inherits it right now; fix it before a room relies on it)")
    errors = ([bad_defaults[k] for k in sorted(used_bad_defaults)]
              + ([default_timeout_err] if bad_default_timeout_used else []) + errors)
    if errors:
        raise ConfigError("Invalid clean room config:\n  - " + "\n  - ".join(errors))
    return rooms, warnings


# --------------------------------------------------------------------------- #
# State store (only needed to detect notebook content changes)
# --------------------------------------------------------------------------- #
class WorkspaceFileState:
    """JSON state kept in a workspace file, e.g. /Workspace/Shared/cleanroom_iac/state.json.
    Records, per notebook asset, the SHA-256 of the source last applied and the etag returned by
    the clean room, so plan can detect both config changes and out-of-band edits."""

    def __init__(self, do: Transport, path: Optional[str]):
        self.do = do
        self.path = self._api_path(path) if path else None
        self.data: dict = {"version": 1, "notebooks": {}}
        self.loaded = False

    @staticmethod
    def _api_path(path: str) -> str:
        return path[len("/Workspace"):] if path.startswith("/Workspace/") else path

    def load(self) -> None:
        if not self.path or self.loaded:
            return
        try:
            res = self.do("GET", "/api/2.0/workspace/export", query={"path": self.path, "format": "SOURCE"})
            self.data = json.loads(base64.b64decode(res.get("content", "")).decode("utf-8") or "{}") or self.data
            self.data.setdefault("notebooks", {})
        except ApiError as e:
            if not e.not_found:
                raise
        self.loaded = True

    def get(self, room: str, asset: str) -> Optional[dict]:
        return self.data["notebooks"].get(room, {}).get(asset)

    def put(self, room: str, asset: str, value: Optional[dict]) -> None:
        if value is None:
            self.data["notebooks"].get(room, {}).pop(asset, None)
        else:
            self.data["notebooks"].setdefault(room, {})[asset] = value

    def drop_room(self, room: str) -> None:
        self.data["notebooks"].pop(room, None)

    def save(self) -> None:
        if not self.path:
            return
        self.do("POST", "/api/2.0/workspace/mkdirs", body={"path": posixpath.dirname(self.path)})
        content = base64.b64encode(json.dumps(self.data, indent=2, sort_keys=True).encode()).decode()
        self.do("POST", "/api/2.0/workspace/import",
                body={"path": self.path, "format": "AUTO", "content": content, "overwrite": True})


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
@dataclass
class Change:
    room: str
    resource_type: str          # CLEAN_ROOM | OUTPUT_CATALOG | ASSET | AUTO_APPROVAL_RULE | GRANT
    resource: str
    action: str                 # CREATE/UPDATE/DELETE/REPLACE/NOOP/DRIFT/WARN/WAIT
    detail: str = ""
    status: str = "PLANNED"     # PLANNED | APPLIED | FAILED | SKIPPED
    error: str = ""
    # True: the room's assets, rules and grants are not planned/applied. Internal only: excluded from
    # comparison/repr and removed in as_row(), the single serialization point for output rows.
    blocking: bool = field(default=False, compare=False, repr=False)
    _fn: Optional[Callable[[], None]] = field(default=None, repr=False, compare=False)

    def as_row(self) -> dict:
        d = asdict(self)
        d.pop("_fn", None)
        d.pop("blocking", None)  # internal flag; keeps the output/run-log schema unchanged
        return d


def _q(s: str) -> str:
    return urllib.parse.quote(str(s), safe="")


class CleanRoomManager:
    def __init__(self, do: Transport, rooms: List[dict], *, state: Optional[WorkspaceFileState] = None,
                 log: Callable[[str], None] = print, sleep: Callable[[float], None] = time.sleep,
                 poll_seconds: int = 15):
        self.do = do
        self.rooms = rooms
        self.state = state or WorkspaceFileState(do, None)
        self.log = log
        self.sleep = sleep
        self.poll_seconds = poll_seconds
        self._metastore: Optional[dict] = None
        self._nb_cache: Dict[str, Tuple[str, str]] = {}

    # ---------------- helpers ----------------
    @property
    def metastore(self) -> dict:
        if self._metastore is None:
            self._metastore = self.do("GET", METASTORE_SUMMARY_API) or {}
        return self._metastore

    def _select(self, names: Optional[List[str]]) -> List[dict]:
        if not names:
            return list(self.rooms)
        wanted = set(names)
        missing = wanted - {r["name"] for r in self.rooms}
        if missing:
            raise ConfigError(f"Rooms not found in config: {sorted(missing)}")
        return [r for r in self.rooms if r["name"] in wanted]

    def _get_room(self, name: str) -> Optional[dict]:
        try:
            room = self.do("GET", f"{CLEAN_ROOMS_API}/{_q(name)}")
        except ApiError as e:
            if e.not_found:
                return None
            raise
        # Deleted rooms stay visible in GET/LIST with status DELETED; treat them as absent.
        if not room or room.get("status") == "DELETED":
            return None
        return room

    def _list(self, path: str, key: str) -> List[dict]:
        items, token = [], None
        while True:
            res = self.do("GET", path, query={"page_token": token} if token else None) or {}
            items.extend(res.get(key) or [])
            token = res.get("next_page_token")
            if not token:
                return items

    def _notebook_source(self, workspace_path: str) -> Tuple[str, str]:
        """Return (html_b64 for the clean room API, sha256 of the SOURCE export)."""
        if workspace_path not in self._nb_cache:
            p = WorkspaceFileState._api_path(workspace_path)
            src = self.do("GET", "/api/2.0/workspace/export", query={"path": p, "format": "SOURCE"})
            sha = hashlib.sha256(base64.b64decode(src.get("content", ""))).hexdigest()
            html = self.do("GET", "/api/2.0/workspace/export", query={"path": p, "format": "HTML"})
            self._nb_cache[workspace_path] = (html.get("content", ""), sha)
        return self._nb_cache[workspace_path]

    # ---------------- room-level ----------------
    def _desired_room_body(self, cfg: dict) -> dict:
        ms = self.metastore
        creator = {"collaborator_alias": cfg["local_alias"], "global_metastore_id": ms.get("global_metastore_id")}
        collaborators = [creator]
        for c in cfg["collaborators"]:
            entry = {"collaborator_alias": c["alias"], "global_metastore_id": c["global_metastore_id"]}
            if c.get("invite_recipient_email"):
                entry["invite_recipient_email"] = c["invite_recipient_email"]
                entry["invite_recipient_workspace_id"] = c["invite_recipient_workspace_id"]
            collaborators.append(entry)
        remote = {
            "cloud_vendor": cfg["cloud_vendor"],
            "region": cfg["region"],
            "collaborators": collaborators,
        }
        if cfg.get("egress_network_policy"):
            remote["egress_network_policy"] = cfg["egress_network_policy"]
        body = {"name": cfg["name"], "remote_detailed_info": remote}
        if cfg.get("comment") is not None:
            body["comment"] = cfg["comment"]
        # The create API accepts only an empty owner or the caller ("Clean room owner must be empty
        # or match the caller"), so ownership is transferred with a PATCH once the room is ACTIVE.
        if cfg.get("enable_shared_output") is not None:
            body["enable_shared_output"] = bool(cfg["enable_shared_output"])
        return copy.deepcopy(body)  # never share nested objects with the parsed config

    def _plan_room(self, cfg: dict, live: Optional[dict]) -> List[Change]:
        name = cfg["name"]
        out: List[Change] = []
        if live is None:
            if not cfg["create"]:
                out.append(Change(name, "CLEAN_ROOM", name, WARN,
                                  "create: false and the room is not visible in this metastore yet. "
                                  "It appears after the creator adds this metastore as a collaborator.",
                                  blocking=True))
                return out
            own_gmid = self.metastore.get("global_metastore_id")
            same = [c["alias"] for c in cfg["collaborators"]
                    if c["global_metastore_id"] == own_gmid and not c.get("invite_recipient_email")]
            if same:
                out.append(Change(name, "CLEAN_ROOM", name, WARN,
                                  f"Collaborator(s) {same} use this metastore's ID. A same-metastore collaborator "
                                  f"needs invite_recipient_email and invite_recipient_workspace_id; otherwise "
                                  f"the API rejects it as a second creator", blocking=True))
                return out
            own_cloud = str(self.metastore.get("cloud") or "").lower()
            if own_cloud and own_cloud != cfg["cloud_vendor"]:
                # Informational only: config stays authoritative and the API makes the final call.
                out.append(Change(name, "CONFIG", "cloud_vendor", WARN,
                                  f"cloud_vendor '{cfg['cloud_vendor']}' differs from this workspace's metastore "
                                  f"cloud '{own_cloud}'. Check this is intended; the API rejects unsupported "
                                  f"cloud/region combinations at apply time"))
            body = self._desired_room_body(cfg)
            peers = ", ".join(f"{c['alias']}={c['global_metastore_id']}" for c in cfg["collaborators"])
            out.append(Change(name, "CLEAN_ROOM", name, CREATE,
                              f"cloud={body['remote_detailed_info']['cloud_vendor']} "
                              f"region={body['remote_detailed_info']['region']} collaborators: {peers}"
                              + (f"; then owner -> {cfg['owner']}" if cfg.get("owner") else ""),
                              _fn=lambda: self.do("POST", CLEAN_ROOMS_API, body=body)))
            return out

        status = live.get("status")
        if status == "FAILED":
            out.append(Change(name, "CLEAN_ROOM", name, WARN,
                              "Room is FAILED. Delete it (destroy mode) and re-apply; not auto-recreated.",
                              blocking=True))
            return out
        if status == "PROVISIONING":
            out.append(Change(name, "CLEAN_ROOM", name, WAIT, "Room is PROVISIONING"))

        remote = live.get("remote_detailed_info") or {}
        live_alias = live.get("local_collaborator_alias")
        if live_alias and live_alias != cfg["local_alias"]:
            out.append(Change(name, "CLEAN_ROOM", name, DRIFT,
                              f"local_alias is '{cfg['local_alias']}' in config but '{live_alias}' live "
                              f"(immutable; fix the config)"))

        # Immutable, creator-owned settings: report only. Changing them needs a new room.
        if cfg["create"]:
            immut = []
            want_cloud = cfg["cloud_vendor"]
            want_region = cfg["region"]
            live_cloud = str(remote.get("cloud_vendor") or "").lower()
            live_region = str(remote.get("region") or "").lower()
            if live_cloud and live_cloud != want_cloud:
                immut.append(f"cloud_vendor {remote.get('cloud_vendor')} -> {want_cloud}")
            if live_region and live_region != want_region:
                immut.append(f"region {remote.get('region')} -> {want_region}")
            live_peers = {c.get("collaborator_alias"): c.get("global_metastore_id")
                          for c in remote.get("collaborators") or []
                          if c.get("collaborator_alias") != live_alias}
            want_peers = {c["alias"]: c["global_metastore_id"] for c in cfg["collaborators"]}
            if remote.get("collaborators") is not None and live_peers != want_peers:
                immut.append(f"collaborators {live_peers} -> {want_peers}")
            want_mode = ((cfg.get("egress_network_policy") or {}).get("internet_access") or {}).get("restriction_mode")
            live_mode = ((remote.get("egress_network_policy") or {}).get("internet_access") or {}).get("restriction_mode")
            if want_mode and live_mode and want_mode != live_mode:
                immut.append(f"egress restriction_mode {live_mode} -> {want_mode}")
            if immut:
                out.append(Change(name, "CLEAN_ROOM", name, DRIFT,
                                  "Immutable settings differ (needs a new room): " + "; ".join(immut)))

        if cfg.get("enable_shared_output") is not None and \
                bool(live.get("enable_shared_output")) != bool(cfg["enable_shared_output"]):
            out.append(Change(name, "CLEAN_ROOM", name, DRIFT,
                              f"enable_shared_output live={live.get('enable_shared_output')} "
                              f"config={cfg['enable_shared_output']} (not auto-updated; verify in UI)"))

        patch = {}
        if cfg.get("comment") is not None and (live.get("comment") or "") != cfg["comment"]:
            patch["comment"] = cfg["comment"]
        if cfg.get("owner") and live.get("owner") != cfg["owner"]:
            patch["owner"] = cfg["owner"]
        if patch:
            detail = ", ".join(f"{k}: {live.get(k)!r} -> {v!r}" for k, v in patch.items())
            out.append(Change(name, "CLEAN_ROOM", name, UPDATE, detail,
                              _fn=lambda: self.do("PATCH", f"{CLEAN_ROOMS_API}/{_q(name)}",
                                                  body={"clean_room": patch})))
        if not any(c.action in (UPDATE, DRIFT, WARN) for c in out):
            out.append(Change(name, "CLEAN_ROOM", name, NOOP, f"status={status}"))
        return out

    def _wait_active(self, name: str, timeout_min: int) -> dict:
        deadline = time.monotonic() + timeout_min * 60
        while True:
            live = self._get_room(name)
            status = (live or {}).get("status")
            if status == "ACTIVE":
                return live
            if status == "FAILED":
                raise ApiError(f"Clean room {name} entered FAILED state")
            if time.monotonic() > deadline:
                raise ApiError(f"Timed out after {timeout_min} min waiting for {name} to be ACTIVE "
                               f"(last status {status})")
            self.log(f"[{name}] waiting for ACTIVE (status={status})")
            self.sleep(self.poll_seconds)

    # ---------------- children ----------------
    def _plan_output_catalog(self, cfg: dict, live: Optional[dict]) -> List[Change]:
        want = cfg.get("output_catalog")
        if not want:
            return []
        name = cfg["name"]
        oc = (live or {}).get("output_catalog") or {}
        if live is not None and oc.get("status") == "CREATED":
            if oc.get("catalog_name") and oc.get("catalog_name") != want:
                return [Change(name, "OUTPUT_CATALOG", want, DRIFT,
                               f"Output catalog already created as '{oc.get('catalog_name')}' (immutable)")]
            return [Change(name, "OUTPUT_CATALOG", want, NOOP)]
        if live is not None and oc.get("status") == "NOT_ELIGIBLE":
            return [Change(name, "OUTPUT_CATALOG", want, WARN, "Output catalog status NOT_ELIGIBLE")]
        return [Change(name, "OUTPUT_CATALOG", want, CREATE,
                       _fn=lambda: self.do("POST", f"{CLEAN_ROOMS_API}/{_q(name)}/output-catalogs",
                                           body={"catalog_name": want}))]

    def _asset_body(self, a: dict) -> dict:
        body = {"name": a["name"], "asset_type": a["type"]}
        if a["type"] == NOTEBOOK:
            html_b64, _ = self._notebook_source(a["workspace_path"])
            nb = {"notebook_content": html_b64, "runner_collaborator_aliases": a["runner_aliases"]}
            if a.get("description") is not None:
                nb["description"] = a["description"]
            if a.get("environment_version"):
                nb["environment_version"] = a["environment_version"]
            body["notebook"] = nb
        else:
            details = {"local_name": a["local_name"]}
            if a.get("partitions") is not None:
                details["partitions"] = a["partitions"]
            body[LOCAL_DETAILS_FIELD[a["type"]]] = details
        return body

    def _record_notebook(self, room: str, a: dict, response: Optional[dict]) -> None:
        _, sha = self._notebook_source(a["workspace_path"])
        etag = ((response or {}).get("notebook") or {}).get("etag")
        self.state.put(room, a["name"], {"sha256": sha, "etag": etag, "workspace_path": a["workspace_path"]})

    def _plan_assets(self, cfg: dict, live: Optional[dict], prune: bool, allow_replace: bool) -> List[Change]:
        room = cfg["name"]
        local = cfg["local_alias"]
        base = f"{CLEAN_ROOMS_API}/{_q(room)}/assets"
        out: List[Change] = []
        live_assets = {}
        if live is not None:
            for a in self._list(base, "assets"):
                if a.get("owner_collaborator_alias") in (None, local):
                    live_assets[(a.get("asset_type"), a.get("name"))] = a

        for a in cfg["assets"]:
            key = (a["type"], a["name"])
            label = f"{a['type']} {a['name']}"
            path = f"{base}/{_q(a['type'])}/{_q(a['name'])}"

            def create(a=a):
                res = self.do("POST", base, body=self._asset_body(a))
                if a["type"] == NOTEBOOK:
                    self._record_notebook(room, a, res)

            if key not in live_assets:
                src = a.get("local_name") or a.get("workspace_path")
                out.append(Change(room, "ASSET", label, CREATE, f"from {src}", _fn=create))
                continue

            detail = self.do("GET", path) or live_assets[key]
            if detail.get("status") == "PERMISSION_DENIED":
                out.append(Change(room, "ASSET", label, WARN,
                                  "PERMISSION_DENIED: the clean room owner lost privileges on the source object"))

            if a["type"] == NOTEBOOK:
                nb = detail.get("notebook") or {}
                reasons = []
                if sorted(nb.get("runner_collaborator_aliases") or []) != a["runner_aliases"]:
                    reasons.append(f"runners {sorted(nb.get('runner_collaborator_aliases') or [])} -> {a['runner_aliases']}")
                if a.get("description") is not None and (nb.get("description") or "") != a["description"]:
                    reasons.append("description changed")
                if a.get("environment_version") and nb.get("environment_version") != a["environment_version"]:
                    reasons.append(f"environment_version {nb.get('environment_version')} -> {a['environment_version']}")
                recorded = self.state.get(room, a["name"]) if self.state.path else None
                if self.state.path:
                    _, sha = self._notebook_source(a["workspace_path"])
                    if not recorded:
                        reasons.append("no recorded state for this notebook (re-upload to baseline)")
                    else:
                        if recorded.get("sha256") != sha:
                            reasons.append("source notebook changed")
                        if recorded.get("etag") and nb.get("etag") and recorded["etag"] != nb["etag"]:
                            reasons.append("edited outside this tool (etag changed)")

                def update(a=a, path=path):
                    res = self.do("PATCH", path, body=self._asset_body(a))
                    self._record_notebook(room, a, res)

                if reasons:
                    out.append(Change(room, "ASSET", label, UPDATE, "; ".join(reasons), _fn=update))
                else:
                    note = "" if self.state.path else "content not compared (no state_path set)"
                    out.append(Change(room, "ASSET", label, NOOP, note))
                continue

            field_name = LOCAL_DETAILS_FIELD[a["type"]]
            live_details = detail.get(field_name) or {}
            live_local = live_details.get("local_name")
            if live_local and live_local != a["local_name"]:
                def replace(a=a, path=path):
                    self.do("DELETE", path)
                    self.do("POST", base, body=self._asset_body(a))
                msg = f"local_name {live_local} -> {a['local_name']}"
                if allow_replace:
                    out.append(Change(room, "ASSET", label, REPLACE, msg, _fn=replace))
                else:
                    out.append(Change(room, "ASSET", label, DRIFT, msg + " (set allow_replace=true to replace)"))
                continue
            if a["type"] == "TABLE" and a.get("partitions") is not None and \
                    _canon(live_details.get("partitions")) != _canon(a["partitions"]):
                out.append(Change(room, "ASSET", label, UPDATE, "partitions changed",
                                  _fn=lambda a=a, path=path: self.do("PATCH", path, body=self._asset_body(a))))
                continue
            out.append(Change(room, "ASSET", label, NOOP))

        wanted = {(a["type"], a["name"]) for a in cfg["assets"]}
        for key, a in sorted(live_assets.items()):
            if key in wanted:
                continue
            label = f"{key[0]} {key[1]}"
            path = f"{base}/{_q(key[0])}/{_q(key[1])}"
            if prune:
                def delete(path=path, key=key):
                    self.do("DELETE", path)
                    if key[0] == NOTEBOOK:
                        self.state.put(room, key[1], None)
                out.append(Change(room, "ASSET", label, DELETE, "not in config (prune=true)", _fn=delete))
            else:
                out.append(Change(room, "ASSET", label, DRIFT, "not in config (prune=false, left as is)"))
        return out

    def _plan_rules(self, cfg: dict, live: Optional[dict], prune: bool) -> List[Change]:
        room = cfg["name"]
        local = cfg["local_alias"]
        base = f"{CLEAN_ROOMS_API}/{_q(room)}/auto-approval-rules"
        out: List[Change] = []
        live_rules = {}
        if live is not None:
            for r in self._list(base, "rules"):
                if r.get("rule_owner_collaborator_alias") in (None, local):
                    k = (r.get("author_collaborator_alias") or f"scope:{r.get('author_scope')}",
                         r.get("runner_collaborator_alias"))
                    live_rules[k] = r

        def label(k):
            return f"author={k[0]} runner={k[1]}"

        wanted = set()
        for ru in cfg["auto_approval_rules"]:
            k = (ru["author_alias"] or f"scope:{ru['author_scope']}", ru["runner_alias"])
            wanted.add(k)
            if k in live_rules:
                out.append(Change(room, "AUTO_APPROVAL_RULE", label(k), NOOP))
                continue
            body = {"runner_collaborator_alias": ru["runner_alias"]}
            if ru["author_alias"]:
                body["author_collaborator_alias"] = ru["author_alias"]
            else:
                body["author_scope"] = ru["author_scope"]
            out.append(Change(room, "AUTO_APPROVAL_RULE", label(k), CREATE,
                              _fn=lambda body=body: self.do("POST", base, body={"auto_approval_rule": body})))
        for k, r in sorted(live_rules.items()):
            if k in wanted:
                continue
            if prune:
                out.append(Change(room, "AUTO_APPROVAL_RULE", label(k), DELETE, "not in config (prune=true)",
                                  _fn=lambda rid=r.get("rule_id"): self.do("DELETE", f"{base}/{_q(rid)}")))
            else:
                out.append(Change(room, "AUTO_APPROVAL_RULE", label(k), DRIFT, "not in config (prune=false)"))
        return out

    def _plan_grants(self, cfg: dict, live: Optional[dict], prune: bool) -> List[Change]:
        room = cfg["name"]
        path = f"{UC_PERMISSIONS_API}/{_q(room)}"
        want = cfg["grants"]
        if not want and not prune:
            return []
        live_grants: Dict[str, set] = {}
        if live is not None:
            res = self.do("GET", path) or {}
            for pa in res.get("privilege_assignments") or []:
                live_grants[pa.get("principal")] = set(pa.get("privileges") or [])
        owner = (live or {}).get("owner") or cfg.get("owner")
        out: List[Change] = []
        principals = set(want) | (set(live_grants) if prune else set())
        for p in sorted(principals):
            if p == owner and p not in want:
                continue
            have = live_grants.get(p, set())
            need = set(want.get(p, []))
            add, remove = sorted(need - have), sorted(have - need)
            if not add and not remove:
                out.append(Change(room, "GRANT", p, NOOP, ",".join(sorted(have))))
                continue
            change = {"principal": p}
            if add:
                change["add"] = add
            if remove:
                change["remove"] = remove
            action = CREATE if not have else (DELETE if not need else UPDATE)
            detail = " ".join(x for x in (f"+{add}" if add else "", f"-{remove}" if remove else "") if x)
            out.append(Change(room, "GRANT", p, action, detail,
                              _fn=lambda change=change: self.do("PATCH", path, body={"changes": [change]})))
        if not prune:
            for p in sorted(set(live_grants) - set(want)):
                if p != owner:
                    out.append(Change(room, "GRANT", p, DRIFT,
                                      f"{sorted(live_grants[p])} not in config (prune=false)"))
        return out

    def _plan_children(self, cfg: dict, live: Optional[dict], prune: bool, allow_replace: bool) -> List[Change]:
        return (self._plan_output_catalog(cfg, live)
                + self._plan_assets(cfg, live, prune, allow_replace)
                + self._plan_rules(cfg, live, prune)
                + self._plan_grants(cfg, live, prune))

    # ---------------- pre-flight (read-only; warnings only; silent when OK) ----------------
    def _me(self) -> dict:
        if not hasattr(self, "_me_cache"):
            self._me_cache = self.do("GET", "/api/2.0/preview/scim/v2/Me") or {}
        return self._me_cache

    def _check(self, out: List[Change], room: str, resource: str, fn: Callable[[], Optional[str]]) -> None:
        """Run one check; a returned string becomes a non-blocking WARN. A failing check never breaks plan."""
        try:
            msg = fn()
        except Exception as e:
            msg = f"could not check ({e}); verify manually"
        if msg:
            out.append(Change(room, PREFLIGHT, resource, WARN, msg))

    def _preflight_global(self, selected: List[dict]) -> List[Change]:
        out: List[Change] = []

        def delta_sharing() -> Optional[str]:
            scope = self.metastore.get("delta_sharing_scope")
            if scope != "INTERNAL_AND_EXTERNAL":
                return (f"Metastore Delta Sharing scope is {scope!r}. Clean Rooms is built on Delta Sharing; "
                        f"if creating a room fails, check that Delta Sharing is enabled for this metastore")
            return None

        def create_privilege() -> Optional[str]:
            me = self._me()
            user = me.get("userName")
            ms_id = self.metastore.get("metastore_id")
            if not user or not ms_id:
                return "could not determine the run-as identity or metastore; verify CREATE_CLEAN_ROOM manually"
            res = self.do("GET", f"/api/2.1/unity-catalog/effective-permissions/METASTORE/{_q(ms_id)}",
                          query={"principal": user}) or {}
            for assignment in res.get("privilege_assignments") or []:
                if any(pr.get("privilege") == "CREATE_CLEAN_ROOM" for pr in assignment.get("privileges") or []):
                    return None
            owner = self.metastore.get("owner")
            my_groups = {g.get("display") for g in me.get("groups") or []}
            if owner and (owner == user or owner in my_groups):
                return None  # metastore admin: implicit privileges are not listed
            return (f"CREATE_CLEAN_ROOM on the metastore was not found for {user}. Creating a room needs it "
                    f"(or metastore admin). Grants through nested groups may not be listed; verify if unsure")

        self._check(out, ALL_ROOMS, "delta_sharing", delta_sharing)
        if any(cfg["create"] for cfg in selected):
            self._check(out, ALL_ROOMS, "CREATE_CLEAN_ROOM", create_privilege)
        return out

    def _owner_check(self, cfg: dict) -> Optional[str]:
        owner = cfg["owner"]
        me = self._me()
        user = me.get("userName")
        if owner == user:
            return None
        if "@" in owner:
            return (f"owner is set to another user ({owner}). After the ownership transfer the run-as identity "
                    f"({user}) can no longer manage this room; use a group that includes it")
        flt = f'displayName eq "{owner}"'
        res = self.do("GET", "/api/2.0/preview/scim/v2/Groups",
                      query={"filter": flt, "attributes": "displayName,members"}) or {}
        groups = res.get("Resources") or []
        if not groups:
            return (f"owner group '{owner}' was not found in this workspace (typo, or an account group not "
                    f"assigned here), so membership of {user} could not be verified. The run-as identity must "
                    f"be a member, or it loses control of the room after the ownership transfer")
        members = {m.get("value") for m in groups[0].get("members") or []}
        my_ids = {me.get("id")} | {g.get("value") for g in me.get("groups") or []}
        if members & my_ids:
            return None
        return (f"{user} is not a direct member of owner group '{owner}' (or of a group listed in it). After the "
                f"ownership transfer it would lose control of the room; add it to the group. Deeper nesting "
                f"is not checked")

    def _source_check(self, asset: dict) -> Optional[str]:
        atype, local = asset["type"], asset["local_name"]
        if atype == "VOLUME":
            self.do("GET", f"/api/2.1/unity-catalog/volumes/{_q(local)}")
            return None
        info = self.do("GET", f"/api/2.1/unity-catalog/tables/{_q(local)}") or {}
        table_type = info.get("table_type")
        if table_type in UNVERIFIED_SOURCE_TYPES:
            return (f"source {local} is a {table_type}. Sharing it as {atype} has not been verified with this "
                    f"tool; check the result in the first apply")
        if table_type and table_type not in EXPECTED_SOURCE_TYPES.get(atype, {table_type}):
            return f"declared as {atype} but source {local} is a {table_type}; check the asset type"
        return None

    def _notebook_check(self, asset: dict) -> Optional[str]:
        path = WorkspaceFileState._api_path(asset["workspace_path"])
        info = self.do("GET", "/api/2.0/workspace/get-status", query={"path": path}) or {}
        if info.get("object_type") != "NOTEBOOK":
            return f"{asset['workspace_path']} is a {info.get('object_type')}, not a notebook"
        return None

    def _preflight_room(self, cfg: dict, live: Optional[dict]) -> List[Change]:
        out: List[Change] = []
        name = cfg["name"]
        owner = cfg.get("owner")
        transfer = owner and ((live is None and cfg["create"]) or (live is not None and live.get("owner") != owner))
        if transfer:
            self._check(out, name, "owner", lambda: self._owner_check(cfg))
        for a in cfg["assets"]:
            label = f"{a['type']} {a['name']}"
            fn = (lambda a=a: self._notebook_check(a)) if a["type"] == NOTEBOOK else (lambda a=a: self._source_check(a))

            def run(fn=fn, a=a):
                try:
                    return fn()
                except ApiError as e:
                    if e.not_found:
                        where = a.get("workspace_path") or a.get("local_name")
                        return f"source {where} was not found (or the run-as identity cannot see it)"
                    raise
            self._check(out, name, label, run)
        return out

    # ---------------- public modes ----------------
    def plan(self, rooms: Optional[List[str]] = None, prune: bool = False,
             allow_replace: bool = False) -> List[Change]:
        self.state.load()
        selected = self._select(rooms)
        changes: List[Change] = self._preflight_global(selected)
        for cfg in selected:
            try:
                live = self._get_room(cfg["name"])
                changes += self._preflight_room(cfg, live)
                room_changes = self._plan_room(cfg, live)
                changes += room_changes
                if any(c.blocking for c in room_changes):
                    continue
                changes += self._plan_children(cfg, live if live and live.get("status") == "ACTIVE" else None,
                                               prune, allow_replace)
            except Exception as e:  # one bad room must not hide the others
                changes.append(Change(cfg["name"], "CLEAN_ROOM", cfg["name"], WARN, "plan failed",
                                      status="FAILED", error=str(e)))
        return changes

    def apply(self, rooms: Optional[List[str]] = None, prune: bool = False,
              allow_replace: bool = False) -> List[Change]:
        self.state.load()
        selected = self._select(rooms)
        results: List[Change] = self._preflight_global(selected)
        for cfg in selected:
            name = cfg["name"]
            try:
                live = self._get_room(name)
                results += self._preflight_room(cfg, live)
                room_changes = self._plan_room(cfg, live)
                self._execute(room_changes)
                results += room_changes
                if any(c.status == "FAILED" for c in room_changes):
                    continue
                if any(c.blocking for c in room_changes):
                    continue
                if live is None or live.get("status") != "ACTIVE":
                    live = self._wait_active(name, cfg["wait_timeout_minutes"])
                    # Post-create / post-provisioning room updates (owner transfer, comment).
                    follow = [c for c in self._plan_room(cfg, live) if c.action == UPDATE]
                    if follow:
                        self._execute(follow)
                        results += follow
                        live = self._get_room(name) or live
                children = self._plan_children(cfg, live, prune, allow_replace)
                self._execute(children)
                results += children
            except Exception as e:
                results.append(Change(name, "CLEAN_ROOM", name, WARN, "apply aborted for this room",
                                      status="FAILED", error=str(e)))
            finally:
                try:
                    self.state.save()
                except Exception as e:  # pragma: no cover
                    results.append(Change(name, "STATE", self.state.path or "", WARN, "state save failed",
                                          status="FAILED", error=str(e)))
        return results

    def destroy(self, rooms: Optional[List[str]], confirm: str) -> List[Change]:
        selected = self._select(rooms)
        expected = ",".join(sorted(r["name"] for r in selected))
        if (confirm or "").strip() != expected:
            raise ConfigError(f"Destroy not confirmed. Set confirm_destroy to exactly: {expected}")
        self.state.load()
        results = []
        for cfg in selected:
            name = cfg["name"]
            live = self._get_room(name)
            if live is None:
                results.append(Change(name, "CLEAN_ROOM", name, NOOP, "already absent", status="APPLIED"))
                continue
            c = Change(name, "CLEAN_ROOM", name, DELETE,
                       "deletes this metastore's clean room securable",
                       _fn=lambda name=name: self.do("DELETE", f"{CLEAN_ROOMS_API}/{_q(name)}"))
            self._execute([c])
            if c.status == "APPLIED":
                self.state.drop_room(name)
            results.append(c)
        self.state.save()
        return results

    def _execute(self, changes: List[Change]) -> None:
        for c in changes:
            if c.action not in MUTATING_ACTIONS or c._fn is None:
                c.status = "SKIPPED" if c.status == "PLANNED" else c.status
                continue
            try:
                self.log(f"[{c.room}] {c.action} {c.resource_type} {c.resource}")
                c._fn()
                c.status = "APPLIED"
            except Exception as e:
                c.status, c.error = "FAILED", str(e)
                self.log(f"[{c.room}] FAILED {c.action} {c.resource_type} {c.resource}: {e}")


def _canon(v: Any) -> str:
    return json.dumps(v, sort_keys=True) if v is not None else "null"


def has_drift(changes: List[Change]) -> bool:
    return any(c.action in MUTATING_ACTIONS | {DRIFT} or c.status == "FAILED" for c in changes)


def format_changes(changes: List[Change]) -> str:
    rows = [("ROOM", "TYPE", "RESOURCE", "ACTION", "STATUS", "DETAIL")]
    for c in changes:
        rows.append((c.room, c.resource_type, c.resource, c.action, c.status,
                     (c.detail + (f" | ERROR: {c.error}" if c.error else ""))[:160]))
    widths = [max(len(str(r[i])) for r in rows) for i in range(5)]
    return "\n".join("  ".join(str(r[i]).ljust(widths[i]) for i in range(5)) + "  " + str(r[5]) for r in rows)


# --------------------------------------------------------------------------- #
# CLI runner (CI pipelines, Terraform terraform_data + local-exec)
# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Manage Databricks Clean Rooms from a config file")
    ap.add_argument("--config", required=True)
    ap.add_argument("--mode", choices=["plan", "apply", "drift", "destroy"], default="plan")
    ap.add_argument("--profile", help="Databricks CLI profile")
    ap.add_argument("--rooms", default="", help="comma-separated subset of room names")
    ap.add_argument("--prune", action="store_true")
    ap.add_argument("--allow-replace", action="store_true")
    ap.add_argument("--state-path", default="", help="workspace path of the JSON state file")
    ap.add_argument("--confirm-destroy", default="")
    args = ap.parse_args(argv)

    try:
        return _run_cli(args)
    except (ConfigError, ApiError) as e:  # expected failures: message only, no traceback
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


def _run_cli(args) -> int:
    rooms_cfg, warnings = normalize_config(load_config_file(args.config))
    for w in warnings:
        print("WARNING:", w)
    do = cli_transport(args.profile)
    mgr = CleanRoomManager(do, rooms_cfg, state=WorkspaceFileState(do, args.state_path or None))
    subset = [r.strip() for r in args.rooms.split(",") if r.strip()] or None
    if args.mode in ("plan", "drift"):
        changes = mgr.plan(subset, args.prune, args.allow_replace)
    elif args.mode == "apply":
        changes = mgr.apply(subset, args.prune, args.allow_replace)
    else:
        changes = mgr.destroy(subset, args.confirm_destroy)
    print(format_changes(changes))
    if any(c.status == "FAILED" for c in changes):
        return 1
    if args.mode == "drift" and has_drift(changes):
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
