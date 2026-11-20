"""Edge-case, transport and CLI tests for cleanroom_iac (offline, stdlib only)."""

import json
import os
import stat
import sys
import tempfile
import textwrap
import types
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import cleanroom_iac as cr  # noqa: E402
from test_cleanroom_iac import FakeApi, base_config, manager, actions, PEER_GMID  # noqa: E402


class RoomStateEdgeTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.api.files["/Shared/nb/overlap"] = b"# Databricks notebook source\nprint(1)\n"

    def _live_room(self, **fields):
        manager(self.api).apply()
        self.api.rooms["acme_partner"].update(fields)

    def test_failed_room_warns_and_skips_children(self):
        self._live_room(status="FAILED")
        changes = manager(self.api).plan()
        self.assertEqual([(c.resource_type, c.action) for c in changes], [("CLEAN_ROOM", "WARN")])
        before = len(self.api.mutations())
        manager(self.api).apply()
        self.assertEqual(len(self.api.mutations()), before)

    def test_provisioning_room_waits_then_converges(self):
        self.api.provision_polls = 3
        results = manager(self.api).apply()
        self.assertFalse([c for c in results if c.status == "FAILED"], cr.format_changes(results))
        self.assertEqual(self.api.rooms["acme_partner"]["status"], "ACTIVE")
        # plan while PROVISIONING shows WAIT and plans children as creates
        api2 = FakeApi(provision_polls=10)
        api2.files["/Shared/nb/overlap"] = b"x"
        api2("POST", cr.CLEAN_ROOMS_API, body=manager(api2)._desired_room_body(
            cr.normalize_config(base_config())[0][0]))
        a = [(c.resource_type, c.action) for c in manager(api2).plan()]
        self.assertIn(("CLEAN_ROOM", "WAIT"), a)
        self.assertIn(("ASSET", "CREATE"), a)

    def test_enable_shared_output_and_egress_drift(self):
        cfg = base_config(enable_shared_output=True,
                          egress_network_policy={"internet_access": {"restriction_mode": "RESTRICTED_ACCESS"}})
        manager(self.api, cfg).apply()
        room = self.api.rooms["acme_partner"]
        self.assertTrue(room["enable_shared_output"])
        self.assertEqual(room["remote_detailed_info"]["egress_network_policy"]["internet_access"]["restriction_mode"],
                         "RESTRICTED_ACCESS")
        self.assertFalse(cr.has_drift(manager(self.api, cfg).plan()))
        room["enable_shared_output"] = False
        room["remote_detailed_info"]["egress_network_policy"]["internet_access"]["restriction_mode"] = "FULL_ACCESS"
        details = " ".join(c.detail for c in manager(self.api, cfg).plan() if c.action == "DRIFT")
        self.assertIn("enable_shared_output", details)
        self.assertIn("egress restriction_mode FULL_ACCESS -> RESTRICTED_ACCESS", details)

    def test_region_and_cloud_override_used_and_compared(self):
        cfg = base_config(cloud_vendor="azure", region="westus")
        manager(self.api, cfg).apply()
        self.assertEqual(self.api.rooms["acme_partner"]["remote_detailed_info"]["region"], "westus")
        self.assertFalse(cr.has_drift(manager(self.api, cfg).plan()))
        details = " ".join(c.detail for c in manager(self.api).plan() if c.action == "DRIFT")
        self.assertIn("region westus -> eastus", details)

    def test_local_alias_mismatch_reported(self):
        self._live_room(local_collaborator_alias="other")
        self.assertTrue(any("local_alias" in c.detail for c in manager(self.api).plan()))

    def test_output_catalog_states(self):
        self._live_room(output_catalog={"status": "CREATED", "catalog_name": "something_else"})
        a = actions(manager(self.api).plan())
        self.assertEqual(a[("OUTPUT_CATALOG", "acme_partner_out")], "DRIFT")
        self.api.rooms["acme_partner"]["output_catalog"] = {"status": "NOT_ELIGIBLE"}
        self.assertEqual(actions(manager(self.api).plan())[("OUTPUT_CATALOG", "acme_partner_out")], "WARN")

    def test_permission_denied_asset_warns(self):
        self._live_room()
        self.api.assets["acme_partner"][("TABLE", "acme.cr_demo.members")]["status"] = "PERMISSION_DENIED"
        warns = [c for c in manager(self.api).plan() if c.action == "WARN" and c.resource_type == "ASSET"]
        self.assertEqual(len(warns), 1)
        self.assertIn("PERMISSION_DENIED", warns[0].detail)

    def test_partitions_update(self):
        parts = [{"values": [{"name": "region", "op": "EQUAL", "value": "east"}]}]
        cfg = base_config()
        cfg["clean_rooms"][0]["assets"][0]["partitions"] = parts
        manager(self.api, cfg).apply()
        self.assertFalse(cr.has_drift(manager(self.api, cfg).plan()))
        cfg["clean_rooms"][0]["assets"][0]["partitions"] = [{"values": [{"name": "region", "op": "EQUAL", "value": "west"}]}]
        c = actions(manager(self.api, cfg).plan())
        self.assertEqual(c[("ASSET", "TABLE acme.cr_demo.members")], "UPDATE")
        manager(self.api, cfg).apply()
        rec = self.api.assets["acme_partner"][("TABLE", "acme.cr_demo.members")]
        self.assertEqual(rec["table_local_details"]["partitions"][0]["values"][0]["value"], "west")

    def test_notebook_runner_description_env_changes(self):
        self._live_room()
        cfg = base_config()
        nb = cfg["clean_rooms"][0]["assets"][1]
        nb.update(runner_aliases=["acme", "partner"], description="new", environment_version=4)
        c = next(c for c in manager(self.api, cfg).plan() if c.resource == "NOTEBOOK_FILE overlap")
        self.assertEqual(c.action, "UPDATE")
        for frag in ("runners", "description", "environment_version"):
            self.assertIn(frag, c.detail)
        manager(self.api, cfg).apply()
        self.assertFalse(cr.has_drift(manager(self.api, cfg).plan()))

    def test_notebook_without_state_is_not_content_compared(self):
        manager(self.api, state_path=None).apply()
        self.api.files["/Shared/nb/overlap"] = b"changed"
        c = next(c for c in manager(self.api, state_path=None).plan() if c.resource == "NOTEBOOK_FILE overlap")
        self.assertEqual(c.action, "NOOP")
        self.assertIn("no state_path", c.detail)

    def test_missing_state_entry_triggers_baseline_upload(self):
        manager(self.api, state_path=None).apply()
        c = next(c for c in manager(self.api).plan() if c.resource == "NOTEBOOK_FILE overlap")
        self.assertEqual(c.action, "UPDATE")
        self.assertIn("no recorded state", c.detail)

    def test_state_file_roundtrip_and_destroy_cleans_it(self):
        manager(self.api).apply()
        state = json.loads(self.api.files["/Shared/cr_state/state.json"])
        entry = state["notebooks"]["acme_partner"]["overlap"]
        self.assertEqual(entry["etag"], "etag-1")
        self.assertEqual(len(entry["sha256"]), 64)
        manager(self.api).destroy(None, confirm="acme_partner")
        state = json.loads(self.api.files["/Shared/cr_state/state.json"])
        self.assertNotIn("acme_partner", state["notebooks"])

    def test_any_author_rule_and_rule_prune(self):
        cfg = base_config(auto_approval_rules=[{"author_scope": "ANY_AUTHOR", "runner_alias": "acme"}])
        manager(self.api, cfg).apply()
        rules = list(self.api.rules["acme_partner"].values())
        self.assertEqual(rules[0]["author_scope"], "ANY_AUTHOR")
        self.assertNotIn("author_collaborator_alias", rules[0])
        # partner-owned rule is ignored; extra local rule pruned only with prune
        self.api.rules["acme_partner"]["p1"] = {"rule_id": "p1", "author_collaborator_alias": "acme",
                                                  "runner_collaborator_alias": "partner",
                                                  "rule_owner_collaborator_alias": "partner"}
        self.api.rules["acme_partner"]["l1"] = {"rule_id": "l1", "author_collaborator_alias": "partner",
                                                  "runner_collaborator_alias": "partner",
                                                  "rule_owner_collaborator_alias": "acme"}
        a = actions(manager(self.api, cfg).plan())
        self.assertEqual(a[("AUTO_APPROVAL_RULE", "author=partner runner=partner")], "DRIFT")
        self.assertNotIn(("AUTO_APPROVAL_RULE", "author=acme runner=partner"), a)
        manager(self.api, cfg).apply(prune=True)
        self.assertEqual(set(self.api.rules["acme_partner"]), {"rule-1", "p1"})

    def test_grant_removal_for_listed_principal_and_owner_untouched(self):
        manager(self.api).apply()
        self.api.grants["acme_partner"]["analysts"].add("MODIFY_CLEAN_ROOM")
        self.api.grants["acme_partner"]["cr_admins"] = {"MODIFY_CLEAN_ROOM"}  # owner
        a = actions(manager(self.api).plan(prune=True))
        self.assertEqual(a[("GRANT", "analysts")], "UPDATE")
        self.assertNotIn(("GRANT", "cr_admins"), a)
        manager(self.api).apply(prune=True)
        self.assertEqual(self.api.grants["acme_partner"]["analysts"], {"EXECUTE_CLEAN_ROOM_TASK"})
        self.assertEqual(self.api.grants["acme_partner"]["cr_admins"], {"MODIFY_CLEAN_ROOM"})

    def test_room_subset_and_unknown_room(self):
        cfg = base_config()
        second = json.loads(json.dumps(cfg["clean_rooms"][0]))
        second["name"] = "second_room"
        cfg["clean_rooms"].append(second)
        changes = manager(self.api, cfg).plan(["second_room"])
        self.assertEqual({c.room for c in changes}, {"second_room"})
        with self.assertRaises(cr.ConfigError):
            manager(self.api, cfg).plan(["nope"])

    def test_one_room_failure_does_not_block_others(self):
        cfg = base_config()
        second = json.loads(json.dumps(cfg["clean_rooms"][0]))
        second["name"] = "second_room"
        cfg["clean_rooms"].append(second)
        self.api.fail_on.add(("POST", f"{cr.CLEAN_ROOMS_API}/acme_partner/assets"))
        results = manager(self.api, cfg).apply()
        self.assertTrue(any(c.room == "acme_partner" and c.status == "FAILED" for c in results))
        self.assertFalse(any(c.room == "second_room" and c.status == "FAILED" for c in results))

    def test_names_are_url_encoded(self):
        cfg = base_config(name="room-with-dash")
        manager(self.api, cfg).apply()
        manager(self.api, cfg).plan()
        paths = [p for _, p, _, _ in self.api.calls]
        self.assertIn(f"{cr.CLEAN_ROOMS_API}/room-with-dash/assets/TABLE/acme.cr_demo.members", paths)
        self.assertEqual(cr._q("a/b c"), "a%2Fb%20c")

    def test_pagination(self):
        pages = {None: {"assets": [{"name": "a"}], "next_page_token": "t2"},
                 "t2": {"assets": [{"name": "b"}], "next_page_token": ""}}
        m = cr.CleanRoomManager(lambda method, path, query=None, body=None: pages[(query or {}).get("page_token")],
                                [], log=lambda *_: None)
        self.assertEqual([a["name"] for a in m._list("/x", "assets")], ["a", "b"])

    def test_duplicate_room_and_asset_rejected(self):
        cfg = base_config()
        cfg["clean_rooms"].append(json.loads(json.dumps(cfg["clean_rooms"][0])))
        cfg["clean_rooms"][0]["assets"].append({"type": "TABLE", "local_name": "main_catalog.cr_demo.members"})
        with self.assertRaises(cr.ConfigError) as ctx:
            cr.normalize_config(cfg)
        self.assertIn("duplicate clean room name", str(ctx.exception))
        self.assertIn("duplicate asset", str(ctx.exception))


class TransportTests(unittest.TestCase):
    def _fake_cli(self, script_body):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "databricks")
        with open(path, "w") as fh:
            fh.write("#!/usr/bin/env python3\nimport sys, json\nargs = sys.argv[1:]\n" + textwrap.dedent(script_body))
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        return path, d

    def test_cli_transport_success_args_and_errors(self):
        cli, d = self._fake_cli("""
            open(sys.argv[0] + '.log', 'a').write(json.dumps(args) + '\\n')
            if 'missing' in args[2]:
                sys.stderr.write("Error: Clean Room 'missing' does not exist.\\n"); sys.exit(1)
            if 'boom' in args[2]:
                sys.stderr.write("Error: PERMISSION_DENIED\\n"); sys.exit(1)
            if args[1] == 'delete':
                sys.exit(0)
            print(json.dumps({"ok": True, "args": args}))
        """)
        do = cr.cli_transport(profile="prof", cli=cli)
        res = do("GET", "/api/2.0/x", query={"path": "/a b", "format": "SOURCE"})
        self.assertEqual(res["args"][:3], ["api", "get", "/api/2.0/x?path=%2Fa+b&format=SOURCE"])
        self.assertEqual(res["args"][-2:], ["-p", "prof"])
        res = do("POST", "/api/2.0/y", body={"k": 1})
        self.assertEqual(json.loads(res["args"][res["args"].index("--json") + 1]), {"k": 1})
        self.assertEqual(do("DELETE", "/api/2.0/z"), {})
        with self.assertRaises(cr.ApiError) as ctx:
            do("GET", "/api/2.0/clean-rooms/missing")
        self.assertTrue(ctx.exception.not_found)
        with self.assertRaises(cr.ApiError) as ctx:
            do("GET", "/api/2.0/clean-rooms/boom")
        self.assertFalse(ctx.exception.not_found)

    def test_sdk_transport_maps_errors(self):
        errors_mod = types.ModuleType("databricks.sdk.errors")

        class DatabricksError(Exception):
            def __init__(self, msg, error_code=None):
                super().__init__(msg)
                self.error_code = error_code

        class NotFound(DatabricksError):
            pass

        errors_mod.DatabricksError, errors_mod.NotFound = DatabricksError, NotFound
        saved = {k: sys.modules.get(k) for k in ("databricks", "databricks.sdk", "databricks.sdk.errors")}
        sys.modules["databricks"] = types.ModuleType("databricks")
        sys.modules["databricks.sdk"] = types.ModuleType("databricks.sdk")
        sys.modules["databricks.sdk.errors"] = errors_mod
        try:
            class Api:
                def do(self, method, path, query=None, body=None):
                    if path == "/nf":
                        raise NotFound("gone", "RESOURCE_DOES_NOT_EXIST")
                    if path == "/nf2":
                        raise DatabricksError("Path (/x) doesn't exist.")
                    if path == "/err":
                        raise DatabricksError("denied", "PERMISSION_DENIED")
                    return None

            do = cr.sdk_transport(types.SimpleNamespace(api_client=Api()))
            self.assertEqual(do("DELETE", "/ok"), {})
            for p, nf in (("/nf", True), ("/nf2", True), ("/err", False)):
                with self.assertRaises(cr.ApiError) as ctx:
                    do("GET", p)
                self.assertEqual(ctx.exception.not_found, nf, p)
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v


class CliMainTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi(provision_polls=0)  # ACTIVE on first poll, so main() never sleeps
        self.api.files["/Shared/nb/overlap"] = b"x"
        self.cfg_path = os.path.join(tempfile.mkdtemp(), "cfg.json")
        with open(self.cfg_path, "w") as fh:
            json.dump(base_config(), fh)
        self._orig = cr.cli_transport
        cr.cli_transport = lambda profile=None, cli="databricks": self.api

    def tearDown(self):
        cr.cli_transport = self._orig

    def run_main(self, *args):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cr.main(["--config", self.cfg_path, *args])
        return code, buf.getvalue()

    def test_exit_codes(self):
        code, out = self.run_main("--mode", "drift")
        self.assertEqual(code, 2)
        self.assertIn("CREATE", out)
        self.assertEqual(self.run_main("--mode", "plan")[0], 0)
        self.assertEqual(self.run_main("--mode", "apply", "--state-path", "/Workspace/Shared/s.json")[0], 0)
        self.assertEqual(self.run_main("--mode", "drift", "--state-path", "/Workspace/Shared/s.json")[0], 0)
        self.api.fail_on.add(("PATCH", cr.CLEAN_ROOMS_API))
        self.api.rooms["acme_partner"]["comment"] = "x"
        self.assertEqual(self.run_main("--mode", "apply")[0], 1)

    def test_destroy_via_cli_requires_confirmation(self):
        self.run_main("--mode", "apply")
        with self.assertRaises(cr.ConfigError):
            self.run_main("--mode", "destroy")
        self.assertEqual(self.run_main("--mode", "destroy", "--confirm-destroy", "acme_partner")[0], 0)

    def test_json_and_yaml_loading(self):
        self.assertEqual(cr.load_config_file(self.cfg_path)["version"], 1)
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML not installed locally (it is on Databricks Runtime)")
        here = os.path.join(os.path.dirname(__file__), "..", "config", "cleanrooms.example.yaml")
        with self.assertRaises(cr.ConfigError):  # placeholders must fail validation
            cr.normalize_config(cr.load_config_file(here))


if __name__ == "__main__":
    unittest.main()
