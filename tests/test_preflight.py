"""Pre-flight checks: read-only, non-blocking warnings, silent when everything is OK."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import cleanroom_iac as cr  # noqa: E402
from test_cleanroom_iac import FakeApi, base_config, manager  # noqa: E402


def preflight(changes):
    return {(c.room, c.resource): c.detail for c in changes if c.resource_type == "PREFLIGHT"}


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.api.files["/Shared/nb/overlap"] = b"x"

    def test_silent_when_everything_is_ok(self):
        self.assertEqual(preflight(manager(self.api).plan()), {})

    def test_a1_missing_create_privilege(self):
        self.api.metastore_privileges = ["CREATE_CATALOG"]
        p = preflight(manager(self.api).plan())
        self.assertIn("CREATE_CLEAN_ROOM on the metastore was not found for runner@corp.com", p[("(all rooms)", "CREATE_CLEAN_ROOM")])
        # metastore admin (member of the metastore owner group) is fine without an explicit grant
        self.api.caller_groups = [("gid-ms", "metastore_admins")]
        self.assertNotIn(("(all rooms)", "CREATE_CLEAN_ROOM"), preflight(manager(self.api).plan()))
        # join-only configs don't need the privilege, so it isn't checked
        api = FakeApi()
        api.metastore_privileges = []
        join = base_config(create=False, collaborators=[], output_catalog=None, assets=[])
        self.assertEqual(preflight(manager(api, join).plan()), {})

    def test_a2_delta_sharing_scope(self):
        self.api.sharing_scope = "INTERNAL"
        self.assertIn("Delta Sharing scope is 'INTERNAL'", preflight(manager(self.api).plan())[("(all rooms)", "delta_sharing")])

    def test_a3_owner_group_membership(self):
        self.api.groups = {"cr_admins": ["someone-else"]}
        p = preflight(manager(self.api).plan())
        self.assertIn("is not a direct member of owner group 'cr_admins'", p[("acme_partner", "owner")])
        self.api.groups = {"cr_admins": ["gid-team"]}           # member through a group listed in it
        self.api.caller_groups = [("gid-team", "team")]
        self.assertNotIn(("acme_partner", "owner"), preflight(manager(self.api).plan()))
        self.api.groups = {}                                    # group not visible in the workspace
        self.assertIn("was not found in this workspace", preflight(manager(self.api).plan())[("acme_partner", "owner")])
        api = FakeApi()
        api.files["/Shared/nb/overlap"] = b"x"
        other_user = preflight(manager(api, base_config(owner="someone@corp.com")).plan())
        self.assertIn("owner is set to another user", other_user[("acme_partner", "owner")])
        self.assertEqual(preflight(manager(api, base_config(owner="runner@corp.com")).plan()), {})

    def test_a3_only_when_ownership_will_change(self):
        manager(self.api).apply()                               # room now owned by cr_admins
        self.api.groups = {}                                    # would warn if checked
        self.assertNotIn(("acme_partner", "owner"), preflight(manager(self.api).plan()))

    def test_a4_source_objects(self):
        self.api.missing_objects = {"main_catalog.cr_demo.members"}
        p = preflight(manager(self.api).plan())
        self.assertIn("source main_catalog.cr_demo.members was not found", p[("acme_partner", "TABLE acme.cr_demo.members")])
        api = FakeApi()
        api.files["/Shared/nb/overlap"] = b"x"
        for table_type, fragment in (("STREAMING_TABLE", "is a STREAMING_TABLE. Sharing it as TABLE has not been verified"),
                                     ("MATERIALIZED_VIEW", "is a MATERIALIZED_VIEW"),
                                     ("VIEW", "declared as TABLE but source main_catalog.cr_demo.members is a VIEW")):
            api.table_types = {"main_catalog.cr_demo.members": table_type}
            self.assertIn(fragment, preflight(manager(api).plan())[("acme_partner", "TABLE acme.cr_demo.members")])

    def test_a5_notebook_path(self):
        del self.api.files["/Shared/nb/overlap"]
        p = preflight(manager(self.api).plan())
        self.assertIn("source /Workspace/Shared/nb/overlap was not found", p[("acme_partner", "NOTEBOOK_FILE overlap")])

    def test_warnings_never_block_apply_and_are_read_only(self):
        self.api.metastore_privileges = []
        self.api.groups = {}
        self.api.table_types = {"main_catalog.cr_demo.members": "STREAMING_TABLE"}
        results = manager(self.api).apply()
        self.assertGreaterEqual(len(preflight(results)), 3)
        self.assertIn("acme_partner", self.api.rooms)            # create still happened
        self.assertFalse([c for c in results if c.status == "FAILED"])
        self.assertFalse(cr.has_drift([c for c in results if c.resource_type == "PREFLIGHT"]))
        reads = [(m, p) for m, p, _, _ in self.api.calls if "scim" in p or "effective-permissions" in p
                 or "/unity-catalog/tables/" in p or "get-status" in p]
        self.assertTrue(reads and all(m == "GET" for m, _ in reads))

    def test_a_failing_check_never_breaks_plan(self):
        self.api.fail_on.add(("GET", "/api/2.0/preview/scim/v2/Me"))
        changes = manager(self.api).plan()
        p = preflight(changes)
        self.assertTrue(any("could not check" in d for d in p.values()))
        self.assertIn(("CLEAN_ROOM", "CREATE"), [(c.resource_type, c.action) for c in changes])


class CliErrorTests(unittest.TestCase):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cr.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_config_errors_are_clean(self):
        with tempfile.TemporaryDirectory() as d:
            bad_json = os.path.join(d, "bad.json")
            with open(bad_json, "w") as fh:
                fh.write("{not json")
            invalid = os.path.join(d, "invalid.json")
            with open(invalid, "w") as fh:
                json.dump(base_config(create="false", collaborators=[]), fh)
            for path, fragment in ((bad_json, "Config is not valid JSON"),
                                   (os.path.join(d, "missing.json"), "Cannot read config file"),
                                   (invalid, "'create' must be true or false")):
                code, _, err = self.run_cli("--config", path, "--mode", "plan")
                self.assertEqual(code, 1, path)
                self.assertIn("ERROR:", err)
                self.assertIn(fragment, err)
                self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
