import unittest

from pydantic import ValidationError

from apps.test_platform.input_contracts import validate_runtime_input


class RuntimeInputContractTests(unittest.TestCase):
    def test_accepts_only_the_explicit_execution_boundary_fields(self):
        value = validate_runtime_input(
            {
                "schema_version": "test-runtime-input.v1",
                "variables": {"account_id": "ACCOUNT-1"},
                "performance_mode": "dry_run",
            }
        )
        self.assertEqual(value.variables, {"account_id": "ACCOUNT-1"})
        self.assertEqual(value.performance_mode, "dry_run")

        with self.assertRaisesRegex(ValidationError, "extra_forbidden"):
            validate_runtime_input({"database_connections": {"main": "secret"}})

    def test_rejects_invalid_schema_mode_and_empty_variable_name(self):
        invalid_values = (
            ({"schema_version": "test-runtime-input.v2"}, "schema_version"),
            ({"performance_mode": "benchmark"}, "performance_mode"),
            ({"variables": {"   ": "value"}}, "名称不能为空"),
        )
        for payload, message in invalid_values:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ValidationError, message):
                    validate_runtime_input(payload)

    def test_none_means_an_empty_versioned_bundle_without_shared_state(self):
        first = validate_runtime_input(None)
        second = validate_runtime_input(None)
        first.variables["one"] = 1
        self.assertEqual(first.schema_version, "test-runtime-input.v1")
        self.assertEqual(second.variables, {})


if __name__ == "__main__":
    unittest.main()
