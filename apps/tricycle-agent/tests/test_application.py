from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout

from tricycle_agent import create_capability_catalog, create_registry
from tricycle_agent.cli import main

EXPECTED_COMPONENTS = {
    "tricycle.bohr.compute",
    "tricycle.bohr.submit",
    "tricycle.reaction-database",
    "tricycle.sar",
    "tricycle.rits",
    "tricycle.sampling",
    "tricycle.reactionopt",
    "tricycle.exploration",
}


class AgentApplicationTests(unittest.TestCase):
    def test_bootstrap_registers_every_application_component(self) -> None:
        registry = create_registry()

        self.assertEqual(
            {manifest.plugin_id for manifest in registry.manifests()}, EXPECTED_COMPONENTS
        )

    def test_capability_catalog_binds_to_registered_components(self) -> None:
        catalog = create_capability_catalog()
        component_ids = {manifest.plugin_id for manifest in create_registry().manifests()}

        self.assertGreater(len(catalog.bindings()), 0)
        self.assertTrue(
            all(binding.component_id in component_ids for binding in catalog.bindings())
        )
        self.assertIn("reactionopt.generate", catalog)
        self.assertIn("bohr.compute.plan", catalog)

    def test_plugins_command_emits_json_manifests(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(["plugins"])

        manifests = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            {manifest["plugin_id"] for manifest in manifests}, EXPECTED_COMPONENTS
        )


if __name__ == "__main__":
    unittest.main()
