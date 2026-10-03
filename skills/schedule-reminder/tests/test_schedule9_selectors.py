"""Adapter propagation of generated selector proof outcomes; native policy belongs to Guards."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import offline_support
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "skills/schedule-reminder/scripts/private_data.py"
GENERATOR = ROOT / "tools/make_fixtures.py"


def load_source(name, path):
    namespace = {"__name__": name, "__file__": str(path)}
    exec(compile(path.read_bytes(), str(path), "exec"), namespace)
    return namespace


class Schedule9SelectorTests(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory(prefix="schedule-selectors-")
        self.addCleanup(workspace.cleanup)
        self.area = Path(workspace.name)

    def cases(self, group):
        generator = load_source("schedule9_generator", GENERATOR)
        cases = generator["schedule9_selector_cases"](self.area / self._testMethodName)
        return [case for case in cases if case["group"] == group]

    def exercise(self, case, writer=False):
        module = load_source("schedule9_private_data", SOURCE)
        queries = []
        boundary = offline_support.synthetic_boundary()
        def prove(directory):
            queries.append(("proof", str(directory)))
            if not case["allowed"] and case["group"] != "visibility":
                raise boundary.GitError("synthetic shared selector refusal")
            return SimpleNamespace(root=str(case["root"]), repositories=("example-owner/private-data",),
                                   signature=case["config"])
        def query(argv):
            queries.append(tuple(argv))
            if argv[:3] == ["gh", "repo", "view"]:
                response = json.loads(case["visibility_response"])
                response["nameWithOwner"] = argv[3]
                return json.dumps(response)
            raise AssertionError("Unadmitted command shape")
        boundary.prove_private_companion = prove
        module["_shared_boundary"] = lambda: boundary
        module["_query"] = query
        operation = module["prepare_parent"] if writer else module["prove_private"]
        with patch.dict(os.environ, {}, clear=True):
            if case["allowed"]:
                proof = operation(case["destination"])
                self.assertEqual(proof["visibility"], "PRIVATE")
                self.assertEqual(proof["repositories"], ["example-owner/private-data"])
                self.assertTrue(any(argv[0] == "gh" for argv in queries))
            else:
                with self.assertRaises(ValueError):
                    operation(case["destination"])
                self.assertFalse(case["destination"].parent.exists())
        if case["group"] != "visibility" and not case["allowed"]:
            self.assertFalse(any(argv[0] == "gh" for argv in queries),
                             "Unproven selector must fail before visibility lookup")
        return queries

    def check_group(self, group, writer=False):
        for case in self.cases(group):
            with self.subTest(case=case["id"]):
                self.exercise(case, writer)

    def test_raw_url_and_inactive_branch_selectors_are_rejected(self):
        self.check_group("raw")

    def test_selector_values_require_exact_configured_remote_names(self):
        self.check_group("names")

    def test_absent_selectors_keep_the_existing_private_proof(self):
        self.check_group("absent")

    def test_all_selector_entries_are_checked_before_casefold_or_override(self):
        self.check_group("all_entries")

    def test_named_selectors_still_require_private_visibility(self):
        self.check_group("visibility")

    def test_rejected_selector_cannot_create_runtime_parent(self):
        self.check_group("writer", writer=True)
