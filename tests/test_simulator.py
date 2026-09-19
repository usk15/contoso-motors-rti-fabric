import unittest

from simulator import (
    MACHINES,
    SimulatorConfig,
    build_events,
    make_event,
    connection_string_values,
    validate_connection_settings,
)


class SimulatorTests(unittest.TestCase):
    def test_machine_roster_is_exact(self) -> None:
        self.assertEqual(MACHINES, tuple(f"M-{number:03d}" for number in range(1, 11)))

    def test_normal_event_matches_schema_and_ranges(self) -> None:
        event = make_event("M-001")
        self.assertEqual(
            set(event),
            {
                "machineId", "line", "plant", "timestamp", "temperatureC",
                "vibrationMm", "rpm", "throughputUnits", "status",
            },
        )
        self.assertTrue(60 <= event["temperatureC"] <= 75)
        self.assertTrue(2 <= event["vibrationMm"] <= 5)
        self.assertTrue(1000 <= event["rpm"] <= 1300)
        self.assertIn(event["status"], ("running", "idle"))
        self.assertTrue(event["timestamp"].endswith("Z"))

    def test_only_selected_machine_is_anomalous(self) -> None:
        config = SimulatorConfig(anomaly_machine="M-007", anomaly_window_seconds=60)
        events = {event["machineId"]: event for event in build_events(10, config)}
        self.assertEqual(events["M-007"]["status"], "fault")
        self.assertGreater(events["M-007"]["vibrationMm"], 8)
        self.assertGreaterEqual(events["M-007"]["temperatureC"], 85)
        self.assertNotEqual(events["M-006"]["status"], "fault")

    def test_anomaly_ends_after_window(self) -> None:
        config = SimulatorConfig(anomaly_machine="M-007", anomaly_window_seconds=60)
        events = {event["machineId"]: event for event in build_events(61, config)}
        self.assertNotEqual(events["M-007"]["status"], "fault")
        self.assertLessEqual(events["M-007"]["vibrationMm"], 5)

    def test_baseline_precedes_anomaly(self) -> None:
        config = SimulatorConfig(
            anomaly_machine="M-007",
            anomaly_window_seconds=60,
            anomaly_start_seconds=90,
        )
        baseline = {event["machineId"]: event for event in build_events(10, config)}
        self.assertNotEqual(baseline["M-007"]["status"], "fault")
        self.assertLessEqual(baseline["M-007"]["vibrationMm"], 5)

        during = {event["machineId"]: event for event in build_events(100, config)}
        self.assertEqual(during["M-007"]["status"], "fault")
        self.assertGreater(during["M-007"]["vibrationMm"], 8)

        after = {event["machineId"]: event for event in build_events(151, config)}
        self.assertNotEqual(after["M-007"]["status"], "fault")

    def test_invalid_machine_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SimulatorConfig(anomaly_machine="M-999").validate()

    def test_namespace_hostname_is_not_a_connection_string(self) -> None:
        with self.assertRaisesRegex(ValueError, "complete Event Hub-compatible connection string"):
            validate_connection_settings(
                "example.servicebus.windows.net",
                "example_eh",
            )

    def test_complete_connection_settings_are_accepted(self) -> None:
        resolved_name = validate_connection_settings(
            "Endpoint=sb://example.servicebus.windows.net/;"
            "SharedAccessKeyName=sender;SharedAccessKey=not-a-real-key;"
            "EntityPath=example_eh",
            None,
        )
        self.assertEqual(resolved_name, "example_eh")

    def test_mismatched_entity_path_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "doesn't match"):
            validate_connection_settings(
                "Endpoint=sb://example.servicebus.windows.net/;"
                "SharedAccessKeyName=sender;SharedAccessKey=not-a-real-key;"
                "EntityPath=actual_eh",
                "wrong_eh",
            )

    def test_connection_string_values_preserve_key_value(self) -> None:
        values = connection_string_values(
            "Endpoint=sb://example/;SharedAccessKey=a=b=c;EntityPath=example_eh"
        )
        self.assertEqual(values["SharedAccessKey"], "a=b=c")


if __name__ == "__main__":
    unittest.main()
