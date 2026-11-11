"""Offline regressions for carV2 trip preparation."""

from unittest import IsolatedAsyncioTestCase
from unittest.mock import Mock

from test_config_flow import load_evolute_module, response


class TripPreparationTests(IsolatedAsyncioTestCase):
    async def test_car_v2_trip_preparation_works_without_account_flags(self):
        api = load_evolute_module("client")
        session = Mock()
        client = api.EvoluteClient(session, "test-access", "test-refresh")
        session.request.side_effect = [
            response(200, {"rows": [{"_id": "test-car"}]}),
            response(200, {}),
            response(200, {"imei": "test-imei"}),
        ]
        await client.async_get_cars()

        for running in (False, True):
            with self.subTest(running=running):
                session.get.return_value = response(
                    200,
                    {
                        "isOnline": True,
                        "sensors": {"isParkedOn": True, "isCentralLockingOn": True},
                        "buttons": [{
                            "tag": "trip-preparation",
                            "activateCommand": "tripPreparationOn",
                            "deactivateCommand": "tripPreparationOff",
                            "enabled": True,
                            "status": running,
                        }],
                    },
                )

                telemetry = await client.async_get_car_info("test-car")

                self.assertIs(telemetry["prep_available"], True)
                self.assertIs(telemetry["prep_running"], running)
                disabled = telemetry[api.DISABLED_COMMANDS_KEY]
                self.assertEqual(api.COMMAND_PREPARE in disabled, running)
                self.assertEqual(api.COMMAND_CANCEL in disabled, not running)
        session.post.assert_not_called()
