"""Malformed-config robustness: wrong value types anywhere must give a ConfigError, never a traceback."""
import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import cleanroom_iac as cr  # noqa: E402
from test_cleanroom_iac import base_config, PEER_GMID  # noqa: E402

BAD_VALUES = [["x"], {"k": 1}, 3.5, True, "²", -1, 0, ""]


def full_config():
    cfg = base_config(enable_shared_output=False, wait_timeout_minutes=30,
                      egress_network_policy={"internet_access": {"restriction_mode": "RESTRICTED_ACCESS"}})
    room = cfg["clean_rooms"][0]
    room["collaborators"][0].update(invite_recipient_email="a@b.com", invite_recipient_workspace_id=123)
    room["assets"][0]["partitions"] = [{"values": [{"name": "r", "op": "EQUAL", "value": "e"}]}]
    room["assets"][1]["environment_version"] = "2"
    cfg["defaults"] = {"owner": "g", "cloud_vendor": "azure", "region": "eastus", "wait_timeout_minutes": 30}
    return cfg


def paths(node, prefix=()):
    """Every (path) to a value inside the config, including containers themselves."""
    yield prefix
    if isinstance(node, dict):
        for k, v in node.items():
            yield from paths(v, prefix + (k,))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from paths(v, prefix + (i,))


def set_path(cfg, path, value):
    node = cfg
    for p in path[:-1]:
        node = node[p]
    node[path[-1]] = value


class TypeRobustnessTests(unittest.TestCase):
    def test_every_field_with_every_bad_value_never_crashes(self):
        base = full_config()
        cr.normalize_config(copy.deepcopy(base))  # the base itself is valid
        checked = 0
        for path in paths(base):
            if not path:
                continue
            for bad in BAD_VALUES:
                cfg = copy.deepcopy(base)
                set_path(cfg, path, bad)
                try:
                    cr.normalize_config(cfg)
                except cr.ConfigError:
                    pass
                except Exception as e:  # pragma: no cover - this is the failure being tested for
                    self.fail(f"{type(e).__name__} for {path} = {bad!r}: {e}")
                checked += 1
        self.assertGreater(checked, 400)

    def test_reviewer_inputs_are_config_errors(self):
        cases = {
            "local_alias list": base_config(local_alias=["acme"]),
            "rule alias list": base_config(auto_approval_rules=[{"author_alias": ["partner"], "runner_alias": "acme"}]),
            "grant principal list": base_config(grants=[{"principal": ["data_eng"], "privileges": ["MODIFY_CLEAN_ROOM"]}]),
            "notebook name list": base_config(assets=[{"type": "NOTEBOOK_FILE", "name": ["nb"], "workspace_path": "/x"}]),
            "runner_aliases text": base_config(assets=[{"type": "NOTEBOOK_FILE", "name": "nb", "workspace_path": "/x",
                                                        "runner_aliases": "partner"}]),
            "superscript timeout": base_config(wait_timeout_minutes="²"),
            "superscript ws id": base_config(collaborators=[{"alias": "partner", "global_metastore_id": PEER_GMID,
                                                             "invite_recipient_email": "a@b.com",
                                                             "invite_recipient_workspace_id": "²"}]),
            "quoted shared output": base_config(enable_shared_output="false"),
        }
        for label, cfg in cases.items():
            with self.assertRaises(cr.ConfigError, msg=label):
                cr.normalize_config(cfg)
        cfg = base_config()
        cfg["defaults"] = ["region"]
        with self.assertRaises(cr.ConfigError) as ctx:
            cr.normalize_config(cfg)
        self.assertIn("'defaults' must be a mapping", str(ctx.exception))

    def test_all_type_problems_are_listed_together(self):
        cfg = base_config(local_alias=["acme"], comment=5, enable_shared_output="no")
        with self.assertRaises(cr.ConfigError) as ctx:
            cr.normalize_config(cfg)
        msg = str(ctx.exception)
        for fragment in ("'local_alias' must be text", "'comment' must be text", "'enable_shared_output'"):
            self.assertIn(fragment, msg)

    def test_timeout_rules(self):
        self.assertEqual(cr.normalize_config(base_config(wait_timeout_minutes=45.0))[0][0]["wait_timeout_minutes"], 45)
        self.assertEqual(cr.normalize_config(base_config(wait_timeout_minutes=" 45 "))[0][0]["wait_timeout_minutes"], 45)
        for bad in (0, -5, "-5", 45.5, True):
            with self.assertRaises(cr.ConfigError, msg=repr(bad)):
                cr.normalize_config(base_config(wait_timeout_minutes=bad))
        cfg = base_config(wait_timeout_minutes="")          # empty room value inherits defaults
        cfg["defaults"] = {"wait_timeout_minutes": 90}
        self.assertEqual(cr.normalize_config(cfg)[0][0]["wait_timeout_minutes"], 90)

    def test_bad_default_timeout_reported_once(self):
        cfg = base_config(wait_timeout_minutes=None)
        second = json.loads(json.dumps(cfg["clean_rooms"][0]))
        second["name"] = "second"
        cfg["clean_rooms"].append(second)
        cfg["defaults"] = {"wait_timeout_minutes": "soon"}
        with self.assertRaises(cr.ConfigError) as ctx:
            cr.normalize_config(cfg)
        msg = str(ctx.exception)
        self.assertEqual(msg.count("wait_timeout_minutes"), 1, msg)
        self.assertIn("defaults: 'wait_timeout_minutes' must be a whole number", msg)
        for r in cfg["clean_rooms"]:                           # nobody inherits it -> warning only
            r["wait_timeout_minutes"] = 30
        _, warnings = cr.normalize_config(cfg)
        self.assertTrue(any("defaults: 'wait_timeout_minutes'" in w and "no room inherits" in w for w in warnings))

    def test_numeric_name_must_be_quoted(self):
        with self.assertRaises(cr.ConfigError) as ctx:
            cr.normalize_config(base_config(name=2024))
        self.assertIn("put it in quotes", str(ctx.exception))
        self.assertEqual(cr.normalize_config(base_config(name="2024"))[0][0]["name"], "2024")

    def test_safety_net_wraps_unexpected_errors(self):
        original = cr._normalize_config
        cr._normalize_config = lambda raw: (_ for _ in ()).throw(TypeError("boom"))
        try:
            with self.assertRaises(cr.ConfigError) as ctx:
                cr.normalize_config(base_config())
            self.assertIn("unexpected value (TypeError: boom)", str(ctx.exception))
        finally:
            cr._normalize_config = original


if __name__ == "__main__":
    unittest.main()
