"""Offline tests for cleanroom_iac using an in-memory fake of the Clean Rooms / UC REST APIs.
Run:  python3 -m unittest discover -s tests -v   (from the project folder)"""

import base64
import json
import os
import sys
import unittest
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import cleanroom_iac as cr  # noqa: E402

LOCAL_GMID = "azure:eastus2:11111111-1111-4111-8111-111111111111"
PEER_GMID = "aws:us-east-2:22222222-2222-4222-8222-222222222222"


class FakeApi:
    """Minimal stateful fake. Records every call in self.calls."""

    def __init__(self, provision_polls=1):
        self.rooms, self.assets, self.rules, self.grants, self.files = {}, {}, {}, {}, {}
        self.calls = []
        self.provision_polls = provision_polls
        self._polls = {}
        self._rule_seq = 0
        self.fail_on = set()   # (method, path-prefix) pairs that should raise
        self.caller = "runner@corp.com"
        # pre-flight data: defaults mean "everything OK", so existing scenarios produce no PREFLIGHT rows
        self.caller_id = "id-runner"
        self.caller_groups = []                 # [(id, display)]
        self.metastore_privileges = ["CREATE_CLEAN_ROOM"]
        self.sharing_scope = "INTERNAL_AND_EXTERNAL"
        self.groups = None                      # None: any group exists and contains the caller
        self.table_types = {}                   # full name -> table_type (default MANAGED)
        self.missing_objects = set()

    def __call__(self, method, path, query=None, body=None):
        self.calls.append((method, path, query, body))
        for m, p in self.fail_on:
            if m == method and path.startswith(p):
                raise cr.ApiError(f"injected failure {method} {path}")
        parts = [urllib.parse.unquote(p) for p in path.strip("/").split("/")]
        if path == cr.METASTORE_SUMMARY_API:
            return {"name": "ms", "global_metastore_id": LOCAL_GMID, "cloud": "azure", "region": "eastus2",
                    "metastore_id": "ms-1", "owner": "metastore_admins", "delta_sharing_scope": self.sharing_scope}
        if path == "/api/2.0/preview/scim/v2/Me":
            return {"id": self.caller_id, "userName": self.caller,
                    "groups": [{"value": gid, "display": d} for gid, d in self.caller_groups]}
        if path == "/api/2.0/preview/scim/v2/Groups":
            name = query["filter"].split('"')[1]
            if self.groups is None:
                return {"Resources": [{"displayName": name, "members": [{"value": self.caller_id}]}]}
            if name not in self.groups:
                return {"Resources": []}
            return {"Resources": [{"displayName": name, "members": [{"value": v} for v in self.groups[name]]}]}
        if path.startswith("/api/2.1/unity-catalog/effective-permissions/METASTORE/"):
            return {"privilege_assignments": [{"principal": query["principal"],
                                               "privileges": [{"privilege": p} for p in self.metastore_privileges]}]}
        if path.startswith("/api/2.1/unity-catalog/tables/") or path.startswith("/api/2.1/unity-catalog/volumes/"):
            full = urllib.parse.unquote(path.rsplit("/", 1)[1])
            if full in self.missing_objects:
                raise cr.ApiError(f"Table '{full}' does not exist.", not_found=True)
            return {"full_name": full, "table_type": self.table_types.get(full, "MANAGED")}
        if path.startswith("/api/2.0/workspace/"):
            return self._workspace(method, parts[-1], query, body)
        if path.startswith(cr.UC_PERMISSIONS_API):
            name = parts[-1]
            if name not in self.rooms:
                raise cr.ApiError(f"Clean Room '{name}' does not exist.", not_found=True)
            g = self.grants.setdefault(name, {})
            if method == "GET":
                return {"privilege_assignments": [{"principal": p, "privileges": sorted(v)} for p, v in g.items() if v]}
            for ch in body["changes"]:
                s = g.setdefault(ch["principal"], set())
                s |= set(ch.get("add", []))
                s -= set(ch.get("remove", []))
            return {}
        # /api/2.0/clean-rooms[/name[/sub...]]
        rest = parts[3:]
        if not rest:
            assert method == "POST"
            if body.get("owner") not in (None, "", self.caller):
                raise cr.ApiError("Clean room owner must be empty or match the caller.")
            name = body["name"]
            body = dict(body, owner=self.caller)
            self.rooms[name] = dict(body, status="PROVISIONING", local_collaborator_alias=body["remote_detailed_info"]["collaborators"][0]["collaborator_alias"],
                                    output_catalog={"status": "NOT_CREATED"})
            self._polls[name] = 0
            return {"name": name, "status": "PROVISIONING"}
        name = rest[0]
        if name not in self.rooms or self.rooms[name]["status"] == "GONE":
            raise cr.ApiError(f"Clean room {name} does not exist", not_found=True)
        room = self.rooms[name]
        if len(rest) == 1:
            if method == "GET":
                if room["status"] == "PROVISIONING":
                    self._polls[name] += 1
                    if self._polls[name] > self.provision_polls:
                        room["status"] = "ACTIVE"
                return json.loads(json.dumps(room))
            if method == "PATCH":
                room.update(body["clean_room"])
                return room
            if method == "DELETE":
                room["status"] = "DELETED"
                return {}
        sub = rest[1]
        if sub == "output-catalogs":
            room["output_catalog"] = {"status": "CREATED", "catalog_name": body["catalog_name"]}
            return {}
        if sub == "assets":
            a = self.assets.setdefault(name, {})
            if len(rest) == 2 and method == "GET":
                return {"assets": [dict(v, notebook=None) for v in a.values()]}
            if len(rest) == 2 and method == "POST":
                rec = dict(body, owner_collaborator_alias=room["local_collaborator_alias"], status="ACTIVE")
                if "notebook" in rec:
                    rec["notebook"] = dict(rec["notebook"], etag="etag-1")
                a[(body["asset_type"], body["name"])] = rec
                return rec
            key = (rest[2], rest[3])
            if key not in a:
                raise cr.ApiError("asset not found", not_found=True)
            if method == "GET":
                return a[key]
            if method == "PATCH":
                rec = dict(a[key], **body)
                if "notebook" in rec:
                    n = int(a[key].get("notebook", {}).get("etag", "etag-0").split("-")[1]) + 1
                    rec["notebook"] = dict(rec["notebook"], etag=f"etag-{n}")
                a[key] = rec
                return rec
            if method == "DELETE":
                del a[key]
                return {}
        if sub == "auto-approval-rules":
            r = self.rules.setdefault(name, {})
            if method == "GET":
                return {"rules": list(r.values())}
            if method == "POST":
                self._rule_seq += 1
                rid = f"rule-{self._rule_seq}"
                r[rid] = dict(body["auto_approval_rule"], rule_id=rid,
                              rule_owner_collaborator_alias=room["local_collaborator_alias"])
                return r[rid]
            if method == "DELETE":
                del r[rest[2]]
                return {}
        raise AssertionError(f"unhandled {method} {path}")

    def _workspace(self, method, op, query, body):
        if op == "export":
            path = query["path"]
            if path not in self.files:
                raise cr.ApiError(f"Path ({path}) doesn't exist.", not_found=True)
            content = self.files[path]
            if query.get("format") == "HTML":
                content = b"<html>" + content + b"</html>"
            return {"content": base64.b64encode(content).decode()}
        if op == "import":
            self.files[body["path"]] = base64.b64decode(body["content"])
            return {}
        if op == "mkdirs":
            return {}
        if op == "get-status":
            if query["path"] not in self.files:
                raise cr.ApiError(f"Path ({query['path']}) doesn't exist.", not_found=True)
            return {"path": query["path"], "object_type": "NOTEBOOK"}
        raise AssertionError(op)

    def mutations(self):
        return [(m, p) for m, p, _, _ in self.calls
                if m != "GET" and not p.startswith("/api/2.0/workspace/")]


def base_config(**overrides):
    room = {
        "name": "acme_partner",
        "comment": "managed",
        "owner": "cr_admins",
        "local_alias": "acme",
        "cloud_vendor": "azure",
        "region": "eastus",
        "collaborators": [{"alias": "partner", "global_metastore_id": PEER_GMID}],
        "output_catalog": "acme_partner_out",
        "assets": [
            {"type": "TABLE", "local_name": "main_catalog.cr_demo.members"},
            {"type": "NOTEBOOK_FILE", "name": "overlap", "workspace_path": "/Workspace/Shared/nb/overlap",
             "runner_aliases": ["partner"], "description": "overlap counts"},
        ],
        "auto_approval_rules": [{"author_alias": "partner", "runner_alias": "acme"}],
        "grants": [{"principal": "analysts", "privileges": ["EXECUTE_CLEAN_ROOM_TASK"]},
                   {"principal": "engineers", "privileges": ["MODIFY CLEAN ROOM"]}],
    }
    room.update(overrides)
    return {"version": 1, "clean_rooms": [room]}


def manager(api, cfg=None, state_path="/Workspace/Shared/cr_state/state.json"):
    rooms, _ = cr.normalize_config(cfg or base_config())
    return cr.CleanRoomManager(api, rooms, state=cr.WorkspaceFileState(api, state_path),
                               log=lambda *_: None, sleep=lambda *_: None)


def actions(changes):
    return {(c.resource_type, c.resource): c.action for c in changes}


class ConfigValidationTests(unittest.TestCase):
    def test_valid_config_normalizes_names_and_privileges(self):
        rooms, warnings = cr.normalize_config(base_config())
        r = rooms[0]
        self.assertEqual(r["assets"][0]["name"], "acme.cr_demo.members")  # default shared name
        self.assertEqual(r["grants"]["engineers"], ["MODIFY_CLEAN_ROOM"])   # "MODIFY CLEAN ROOM" normalized
        self.assertEqual(warnings, [])

    def test_collects_all_errors(self):
        bad = base_config(local_alias="", collaborators=[{"alias": "p", "global_metastore_id": "nope",
                                                          "invite_recipient_email": "a@b.com"}],
                          assets=[{"type": "TABLE", "local_name": "two.parts"},
                                  {"type": "NOTEBOOK_FILE", "name": "x"}],
                          auto_approval_rules=[{"author_alias": "ghost", "runner_alias": "acme"}])
        with self.assertRaises(cr.ConfigError) as ctx:
            cr.normalize_config(bad)
        msg = str(ctx.exception)
        for fragment in ["local_alias", "cloud:region:metastore-uuid", "invite_recipient_workspace_id",
                         "catalog.schema.object", "workspace_path", "'ghost'"]:
            self.assertIn(fragment, msg)

    def test_user_owner_and_unknown_privilege_warn(self):
        cfg = base_config(owner="someone@corp.com",
                          grants=[{"principal": "g", "privileges": ["SELECT"]}])
        _, warnings = cr.normalize_config(cfg)
        self.assertEqual(len(warnings), 2)

    def test_cloud_and_region_required_for_created_rooms(self):
        cfg = base_config()
        del cfg["clean_rooms"][0]["region"]
        del cfg["clean_rooms"][0]["cloud_vendor"]
        with self.assertRaises(cr.ConfigError) as ctx:
            cr.normalize_config(cfg)
        self.assertIn("'region'", str(ctx.exception))
        self.assertIn("'cloud_vendor'", str(ctx.exception))
        cfg["defaults"] = {"cloud_vendor": "gcp", "region": "us-central1"}   # set once for all rooms
        room = cr.normalize_config(cfg)[0][0]
        self.assertEqual((room["cloud_vendor"], room["region"]), ("gcp", "us-central1"))
        with self.assertRaises(cr.ConfigError):
            cr.normalize_config(base_config(cloud_vendor="oracle"))
        with self.assertRaises(cr.ConfigError):
            cr.normalize_config(base_config(region="East US 2"))
        # join-only rooms don't need either
        cr.normalize_config(base_config(create=False, collaborators=[], region=None, cloud_vendor=None))

    def test_cloud_and_region_are_normalized(self):
        room = cr.normalize_config(base_config(cloud_vendor=" Azure ", region="EastUS"))[0][0]
        self.assertEqual((room["cloud_vendor"], room["region"]), ("azure", "eastus"))
        for bad in ("-", "--", "us--west", "-eastus", "east us", "east_us"):
            with self.assertRaises(cr.ConfigError, msg=bad):
                cr.normalize_config(base_config(region=bad))
        for good in ("eastus", "us-west-2", "europe-west1"):
            cr.normalize_config(base_config(region=good))

    def test_empty_room_value_inherits_defaults(self):
        cfg = base_config(cloud_vendor=None, region="")
        cfg["defaults"] = {"cloud_vendor": "AWS", "region": "us-east-1"}
        room = cr.normalize_config(cfg)[0][0]
        self.assertEqual((room["cloud_vendor"], room["region"]), ("aws", "us-east-1"))

    def test_live_case_difference_is_not_drift(self):
        api = FakeApi()
        api.files["/Shared/nb/overlap"] = b"x"
        manager(api).apply()
        api.rooms["acme_partner"]["remote_detailed_info"].update(
            cloud_vendor="AZURE", region="EastUS")
        self.assertFalse(cr.has_drift(manager(api, base_config(cloud_vendor="Azure", region="eastus")).plan()))

    def test_non_string_cloud_or_region_rejected(self):
        for kw in ({"cloud_vendor": True}, {"region": 123}, {"region": ["eastus"]}):
            with self.assertRaises(cr.ConfigError) as ctx:
                cr.normalize_config(base_config(**kw))
            self.assertIn("must be a string", str(ctx.exception), kw)

    def test_join_only_rooms_ignore_cloud_and_region_types(self):
        cfg = base_config(create=False, collaborators=[], cloud_vendor=True, region=123)
        cfg["defaults"] = {"region": False}          # bad default used by nobody
        room = cr.normalize_config(cfg)[0][0]
        self.assertEqual((room["cloud_vendor"], room["region"]), (None, None))
        # a valid value on a join-only room is still kept (normalized), as before
        room = cr.normalize_config(base_config(create=False, collaborators=[], region="EastUS"))[0][0]
        self.assertEqual(room["region"], "eastus")

    def test_bad_default_reported_once_and_only_when_used(self):
        cfg = base_config(cloud_vendor=None, region=None)
        for n in range(1, 6):                          # 6 created rooms inherit the bad default
            extra = json.loads(json.dumps(cfg["clean_rooms"][0]))
            extra["name"] = f"room_{n}"
            cfg["clean_rooms"].append(extra)
        cfg["defaults"] = {"cloud_vendor": "azure", "region": 123}
        with self.assertRaises(cr.ConfigError) as ctx:
            cr.normalize_config(cfg)
        msg = str(ctx.exception)
        self.assertEqual(msg.count("must be a string"), 1, msg)
        self.assertIn("defaults: 'region' must be a string, got int 123", msg)
        self.assertNotIn("'region' of the central clean room is required", msg)   # no follow-on noise
        # every created room overrides the bad default -> no error at all
        for r in cfg["clean_rooms"]:
            r["region"] = "eastus"
        cr.normalize_config(cfg)

    def test_join_only_bad_types_warn_not_fail(self):
        cfg = base_config(create=False, collaborators=[], cloud_vendor=True, region=123)
        rooms, warnings = cr.normalize_config(cfg)
        self.assertEqual((rooms[0]["cloud_vendor"], rooms[0]["region"]), (None, None))
        joined = " ".join(warnings)
        self.assertIn("'cloud_vendor' must be a string", joined)
        self.assertIn("'region' must be a string", joined)
        self.assertIn("ignored because create: false", joined)

    def test_join_only_misspelled_values_warn(self):
        cfg = base_config(create=False, collaborators=[], cloud_vendor="azrue", region="East US")
        rooms, warnings = cr.normalize_config(cfg)
        joined = " ".join(warnings)
        self.assertIn("'cloud_vendor' must be one of", joined)
        self.assertIn("'region' is not a valid region identifier", joined)
        self.assertEqual(joined.count("ignored because create: false"), 2)
        _, warnings = cr.normalize_config(base_config(create=False, collaborators=[], region="eastus"))
        self.assertFalse([w for w in warnings if "region" in w])          # valid value: no warning

    def test_bad_default_only_used_by_join_only_rooms_warns_once(self):
        cfg = base_config(create=False, collaborators=[], cloud_vendor=None, region=None)
        second = json.loads(json.dumps(cfg["clean_rooms"][0]))
        second["name"] = "joined_2"
        cfg["clean_rooms"].append(second)
        cfg["defaults"] = {"cloud_vendor": "azrue"}
        _, warnings = cr.normalize_config(cfg)                              # no error
        hits = [w for w in warnings if "defaults: 'cloud_vendor' must be one of" in w]
        self.assertEqual(len(hits), 1, warnings)
        self.assertIn("only inherited by create: false rooms", hits[0])
        # once a created room inherits it, it is an error (reported once) and not also a warning
        cfg["clean_rooms"].append(base_config(cloud_vendor=None)["clean_rooms"][0])
        with self.assertRaises(cr.ConfigError) as ctx:
            cr.normalize_config(cfg)
        self.assertEqual(str(ctx.exception).count("defaults: 'cloud_vendor'"), 1)

    def test_create_must_be_a_real_boolean(self):
        for bad in ("false", "true", "no", 0, 1, None):
            with self.assertRaises(cr.ConfigError, msg=repr(bad)) as ctx:
                cr.normalize_config(base_config(create=bad, collaborators=[]))
            self.assertIn("'create' must be true or false without quotes", str(ctx.exception))
        # an invalid value never turns into a create: no follow-on "collaborator required" noise
        with self.assertRaises(cr.ConfigError) as ctx:
            cr.normalize_config(base_config(create="false", collaborators=[], cloud_vendor=None, region=None))
        self.assertNotIn("at least one other collaborator", str(ctx.exception))
        self.assertNotIn("'region' of the central clean room is required", str(ctx.exception))
        self.assertTrue(cr.normalize_config(base_config(create=True))[0][0]["create"])
        self.assertFalse(cr.normalize_config(base_config(create=False, collaborators=[]))[0][0]["create"])

    def test_bad_numbers_are_collected_not_raised(self):
        cfg = base_config(wait_timeout_minutes="soon",
                          collaborators=[{"alias": "partner", "global_metastore_id": PEER_GMID,
                                          "invite_recipient_email": "a@b.com", "invite_recipient_workspace_id": "abc"}],
                          assets=[{"type": "TABLE", "local_name": "two.parts"}])
        with self.assertRaises(cr.ConfigError) as ctx:          # ConfigError, not ValueError
            cr.normalize_config(cfg)
        msg = str(ctx.exception)
        self.assertIn("invite_recipient_workspace_id must be a whole number (got 'abc')", msg)
        self.assertIn("'wait_timeout_minutes' must be a whole number (got 'soon')", msg)
        self.assertIn("catalog.schema.object", msg)              # other problems still listed
        with self.assertRaises(cr.ConfigError):
            cr.normalize_config(base_config(wait_timeout_minutes=True))
        room = cr.normalize_config(base_config(wait_timeout_minutes="45"))[0][0]
        self.assertEqual(room["wait_timeout_minutes"], 45)
        room = cr.normalize_config(base_config(wait_timeout_minutes=None))[0][0]
        self.assertEqual(room["wait_timeout_minutes"], 30)

    def test_malformed_entries_are_config_errors(self):
        cases = {"collaborators": ["partner_a"], "assets": ["a.b.c"], "auto_approval_rules": ["r"],
                 "grants": ["g"]}
        for key, value in cases.items():
            with self.assertRaises(cr.ConfigError, msg=key) as ctx:   # not AttributeError
                cr.normalize_config(base_config(**{key: value}))
            self.assertIn(f"{key}[0] must be a mapping", str(ctx.exception))
        for key in ("collaborators", "assets", "auto_approval_rules", "grants"):
            with self.assertRaises(cr.ConfigError, msg=key) as ctx:
                cr.normalize_config(base_config(**{key: {"not": "a list"}}))
            self.assertIn(f"'{key}' must be a list", str(ctx.exception))
        with self.assertRaises(cr.ConfigError) as ctx:
            cr.normalize_config(base_config(grants=[{"principal": "g", "privileges": "MODIFY_CLEAN_ROOM"}]))
        self.assertIn("privileges must be a list", str(ctx.exception))
        with self.assertRaises(cr.ConfigError) as ctx:                # null entry: same as before
            cr.normalize_config(base_config(assets=[None]))
        self.assertIn("type must be one of", str(ctx.exception))

    def test_unused_bad_default_warns(self):
        cfg = base_config()                                           # room sets its own cloud/region
        cfg["defaults"] = {"region": "East US", "cloud_vendor": 5}
        _, warnings = cr.normalize_config(cfg)
        joined = " ".join(warnings)
        self.assertIn("defaults: 'region' is not a valid region identifier", joined)
        self.assertIn("defaults: 'cloud_vendor' must be a string", joined)
        self.assertEqual(joined.count("no room inherits it right now"), 2)

    def test_join_only_invalid_value_is_not_stored(self):
        rooms, _ = cr.normalize_config(base_config(create=False, collaborators=[],
                                                   cloud_vendor="azrue", region="East US"))
        self.assertEqual((rooms[0]["cloud_vendor"], rooms[0]["region"]), (None, None))

    def test_misspelled_default_reported_once(self):
        cfg = base_config(cloud_vendor=None, region=None)
        for n in range(1, 6):
            extra = json.loads(json.dumps(cfg["clean_rooms"][0]))
            extra["name"] = f"room_{n}"
            cfg["clean_rooms"].append(extra)
        cfg["defaults"] = {"cloud_vendor": "azrue", "region": "East US"}
        with self.assertRaises(cr.ConfigError) as ctx:
            cr.normalize_config(cfg)
        msg = str(ctx.exception)
        self.assertEqual(msg.count("defaults: 'cloud_vendor' must be one of"), 1, msg)
        self.assertEqual(msg.count("defaults: 'region' is not a valid region identifier"), 1, msg)
        self.assertNotIn("clean room 'room_", msg)            # no per-room repeats
        for r in cfg["clean_rooms"]:                           # unused bad defaults -> no error
            r.update(cloud_vendor="azure", region="eastus")
        cr.normalize_config(cfg)

    def test_blocking_does_not_affect_equality_or_repr(self):
        a = cr.Change("r", "CLEAN_ROOM", "r", "WARN", "x", blocking=True)
        b = cr.Change("r", "CLEAN_ROOM", "r", "WARN", "x")
        self.assertEqual(a, b)
        self.assertNotIn("blocking", repr(a))
        self.assertNotIn("blocking", a.as_row())

    def test_room_level_bad_value_names_the_room(self):
        with self.assertRaises(cr.ConfigError) as ctx:
            cr.normalize_config(base_config(region=["eastus"]))
        self.assertIn("clean room 'acme_partner': 'region' must be a string, got list", str(ctx.exception))

    def test_blocking_is_an_explicit_flag(self):
        api = FakeApi()
        api.files["/Shared/nb/overlap"] = b"x"
        same_ms = base_config(collaborators=[{"alias": "partner", "global_metastore_id": LOCAL_GMID}])
        self.assertTrue(all(c.blocking for c in manager(api, same_ms).plan()))
        join = base_config(create=False, collaborators=[], output_catalog=None)
        self.assertTrue(all(c.blocking for c in manager(api, join).plan()))
        manager(api).apply()
        api.rooms["acme_partner"]["status"] = "FAILED"
        self.assertEqual([(c.action, c.blocking) for c in manager(api).plan()], [("WARN", True)])
        # non-blocking warnings, whatever their label, never stop child planning
        cross = [c for c in manager(FakeApi(), base_config(cloud_vendor="aws", region="us-west-2")).plan()
                 if c.action == "WARN"]
        self.assertTrue(cross and not any(c.blocking for c in cross))
        self.assertNotIn("blocking", cross[0].as_row())          # output schema unchanged

    def test_cross_cloud_warns_but_does_not_block(self):
        api = FakeApi()  # fake metastore cloud is azure
        api.files["/Shared/nb/overlap"] = b"x"
        cfg = base_config(cloud_vendor="aws", region="us-west-2")
        plan = manager(api, cfg).plan()
        warns = [c for c in plan if c.resource_type == "CONFIG" and c.action == "WARN"]
        self.assertEqual(len(warns), 1)
        self.assertIn("differs from this workspace's metastore cloud 'azure'", warns[0].detail)
        self.assertIn(("ASSET", "CREATE"), [(c.resource_type, c.action) for c in plan])   # children still planned
        results = manager(api, cfg).apply()
        self.assertIn("acme_partner", api.rooms)                                     # create still happens
        self.assertFalse([c for c in results if c.status == "FAILED"])

    def test_same_cloud_has_no_warning(self):
        api = FakeApi()
        api.files["/Shared/nb/overlap"] = b"x"
        self.assertFalse([c for c in manager(api).plan() if c.resource_type == "CONFIG"])

    def test_cloud_and_region_never_taken_from_metastore(self):
        # The fake metastore is azure/eastus2; config says aws/us-west-2. Only config may be used.
        cfg = base_config(cloud_vendor="aws", region="us-west-2")
        api = FakeApi()
        api.files["/Shared/nb/overlap"] = b"x"
        manager(api, cfg).apply()
        post = next(b for m, p, _, b in api.calls if m == "POST" and p == cr.CLEAN_ROOMS_API)
        self.assertEqual((post["remote_detailed_info"]["cloud_vendor"], post["remote_detailed_info"]["region"]),
                         ("aws", "us-west-2"))
        self.assertFalse(cr.has_drift(manager(api, cfg).plan()))

    def test_create_requires_a_collaborator(self):
        with self.assertRaises(cr.ConfigError):
            cr.normalize_config(base_config(collaborators=[]))
        cr.normalize_config(base_config(collaborators=[], create=False))  # join-only is fine


class PlanApplyTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.api.files["/Shared/nb/overlap"] = b"# Databricks notebook source\nprint(1)\n"

    def test_plan_for_new_room_is_read_only_and_complete(self):
        changes = manager(self.api).plan()
        self.assertEqual(self.api.mutations(), [])
        a = actions(changes)
        self.assertEqual(a[("CLEAN_ROOM", "acme_partner")], "CREATE")
        self.assertEqual(a[("ASSET", "TABLE acme.cr_demo.members")], "CREATE")
        self.assertEqual(a[("ASSET", "NOTEBOOK_FILE overlap")], "CREATE")
        self.assertEqual(a[("GRANT", "analysts")], "CREATE")
        self.assertEqual(a[("OUTPUT_CATALOG", "acme_partner_out")], "CREATE")

    def test_create_body_uses_runtime_metastore_and_no_creator_email(self):
        manager(self.api).apply()
        post = next(b for m, p, _, b in self.api.calls if m == "POST" and p == cr.CLEAN_ROOMS_API)
        remote = post["remote_detailed_info"]
        self.assertEqual(remote["cloud_vendor"], "azure")
        self.assertEqual(remote["region"], "eastus")
        self.assertEqual(remote["collaborators"][0],
                         {"collaborator_alias": "acme", "global_metastore_id": LOCAL_GMID})
        self.assertEqual(remote["collaborators"][1]["global_metastore_id"], PEER_GMID)

    def test_owner_is_transferred_after_create(self):
        manager(self.api).apply()
        post = next(b for m, p, _, b in self.api.calls if m == "POST" and p == cr.CLEAN_ROOMS_API)
        self.assertNotIn("owner", post)
        self.assertEqual(self.api.rooms["acme_partner"]["owner"], "cr_admins")
        patch = [b for m, p, _, b in self.api.calls if m == "PATCH" and p == f"{cr.CLEAN_ROOMS_API}/acme_partner"]
        self.assertEqual(patch, [{"clean_room": {"owner": "cr_admins", }}])

    def test_same_metastore_collaborator_needs_invite(self):
        cfg = base_config(collaborators=[{"alias": "partner", "global_metastore_id": LOCAL_GMID}])
        changes = manager(self.api, cfg).apply()
        self.assertEqual([c.action for c in changes], ["WARN"])
        self.assertIn("invite_recipient_email", changes[0].detail)
        self.assertEqual(self.api.mutations(), [])
        cfg = base_config(collaborators=[{"alias": "partner", "global_metastore_id": LOCAL_GMID,
                                          "invite_recipient_email": "a@b.com",
                                          "invite_recipient_workspace_id": 123}])
        a = actions(manager(self.api, cfg).plan())
        self.assertEqual(a[("CLEAN_ROOM", "acme_partner")], "CREATE")

    def test_apply_then_plan_is_noop(self):
        results = manager(self.api).apply()
        self.assertFalse([c for c in results if c.status == "FAILED"], [c.as_row() for c in results])
        self.assertEqual(self.api.grants["acme_partner"]["engineers"], {"MODIFY_CLEAN_ROOM"})
        plan = manager(self.api).plan()
        self.assertFalse(cr.has_drift(plan), cr.format_changes(plan))

    def test_apply_is_idempotent(self):
        manager(self.api).apply()
        before = len(self.api.mutations())
        manager(self.api).apply()
        self.assertEqual(len(self.api.mutations()), before, "second apply must not mutate anything")

    def test_deleted_room_is_treated_as_absent(self):
        manager(self.api).apply()
        self.api.rooms["acme_partner"]["status"] = "DELETED"
        a = actions(manager(self.api).plan())
        self.assertEqual(a[("CLEAN_ROOM", "acme_partner")], "CREATE")

    def test_comment_and_owner_update(self):
        manager(self.api).apply()
        self.api.rooms["acme_partner"]["comment"] = "edited in UI"
        a = actions(manager(self.api).plan())
        self.assertEqual(a[("CLEAN_ROOM", "acme_partner")], "UPDATE")
        manager(self.api).apply()
        self.assertEqual(self.api.rooms["acme_partner"]["comment"], "managed")

    def test_immutable_collaborator_change_is_drift_only(self):
        manager(self.api).apply()
        cfg = base_config(collaborators=[{"alias": "partner",
                                          "global_metastore_id": "gcp:us-central1:00000000-0000-0000-0000-000000000000"}])
        changes = manager(self.api, cfg).plan()
        room_actions = [c.action for c in changes if c.resource_type == "CLEAN_ROOM"]
        self.assertIn("DRIFT", room_actions)
        self.assertNotIn("CREATE", room_actions)

    def test_notebook_change_and_out_of_band_edit_detected(self):
        manager(self.api).apply()
        self.api.files["/Shared/nb/overlap"] = b"# Databricks notebook source\nprint(2)\n"
        c = next(c for c in manager(self.api).plan() if c.resource == "NOTEBOOK_FILE overlap")
        self.assertEqual((c.action, c.detail), ("UPDATE", "source notebook changed"))
        manager(self.api).apply()
        self.assertFalse(cr.has_drift(manager(self.api).plan()))
        self.api.assets["acme_partner"][("NOTEBOOK_FILE", "overlap")]["notebook"]["etag"] = "etag-99"
        c = next(c for c in manager(self.api).plan() if c.resource == "NOTEBOOK_FILE overlap")
        self.assertIn("edited outside this tool", c.detail)

    def test_unmanaged_items_drift_without_prune_and_delete_with_prune(self):
        manager(self.api).apply()
        self.api.assets["acme_partner"][("TABLE", "acme.cr_demo.extra")] = {
            "asset_type": "TABLE", "name": "acme.cr_demo.extra", "owner_collaborator_alias": "acme",
            "table_local_details": {"local_name": "main_catalog.cr_demo.extra"}}
        self.api.assets["acme_partner"][("TABLE", "partner.p.t")] = {
            "asset_type": "TABLE", "name": "partner.p.t", "owner_collaborator_alias": "partner"}
        self.api.grants["acme_partner"]["stranger"] = {"MODIFY_CLEAN_ROOM"}
        a = actions(manager(self.api).plan())
        self.assertEqual(a[("ASSET", "TABLE acme.cr_demo.extra")], "DRIFT")
        self.assertNotIn(("ASSET", "TABLE partner.p.t"), a)  # partner-owned assets are never touched
        self.assertEqual(a[("GRANT", "stranger")], "DRIFT")
        manager(self.api).apply(prune=True)
        self.assertNotIn(("TABLE", "acme.cr_demo.extra"), self.api.assets["acme_partner"])
        self.assertIn(("TABLE", "partner.p.t"), self.api.assets["acme_partner"])
        self.assertEqual(self.api.grants["acme_partner"]["stranger"], set())

    def test_local_name_change_needs_allow_replace(self):
        manager(self.api).apply()
        cfg = base_config()
        cfg["clean_rooms"][0]["assets"][0] = {"type": "TABLE", "local_name": "main_catalog.cr_v2.members",
                                              "name": "acme.cr_demo.members"}
        self.assertEqual(actions(manager(self.api, cfg).plan())[("ASSET", "TABLE acme.cr_demo.members")], "DRIFT")
        manager(self.api, cfg).apply(allow_replace=True)
        rec = self.api.assets["acme_partner"][("TABLE", "acme.cr_demo.members")]
        self.assertEqual(rec["table_local_details"]["local_name"], "main_catalog.cr_v2.members")

    def test_partial_failure_is_reported_and_rerun_converges(self):
        self.api.fail_on.add(("PATCH", cr.UC_PERMISSIONS_API))
        results = manager(self.api).apply()
        failed = [c for c in results if c.status == "FAILED"]
        self.assertEqual({c.resource_type for c in failed}, {"GRANT"})
        self.assertTrue(any(c.status == "APPLIED" and c.resource_type == "ASSET" for c in results))
        self.api.fail_on.clear()
        manager(self.api).apply()
        self.assertFalse(cr.has_drift(manager(self.api).plan()))

    def test_join_only_room_is_not_created(self):
        cfg = base_config(create=False, collaborators=[], output_catalog=None)
        changes = manager(self.api, cfg).apply()
        self.assertEqual([c.action for c in changes], ["WARN"])
        self.assertEqual(self.api.mutations(), [])

    def test_destroy_requires_exact_confirmation(self):
        manager(self.api).apply()
        with self.assertRaises(cr.ConfigError):
            manager(self.api).destroy(None, confirm="yes")
        res = manager(self.api).destroy(None, confirm="acme_partner")
        self.assertEqual(res[0].status, "APPLIED")
        self.assertEqual(self.api.rooms["acme_partner"]["status"], "DELETED")
        self.assertEqual(manager(self.api).destroy(None, confirm="acme_partner")[0].action, "NOOP")

    def test_wait_times_out(self):
        api = FakeApi(provision_polls=10 ** 6)
        api.files["/Shared/nb/overlap"] = b"x"
        m = manager(api)
        orig = cr.time.monotonic
        ticks = iter(range(0, 10 ** 6, 600))
        cr.time.monotonic = lambda: next(ticks)
        try:
            results = m.apply()
        finally:
            cr.time.monotonic = orig
        self.assertTrue(any("Timed out" in c.error for c in results))


if __name__ == "__main__":
    unittest.main()
