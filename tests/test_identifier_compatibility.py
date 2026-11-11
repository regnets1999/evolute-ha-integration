"""Offline regressions for an in-place update from Evolute 1.4.3 (fb87fcc)."""

from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from test_config_flow import fake_access_jwt, load_evolute_module, response


CAR_ID = "c0ffee000000000000000001"
VIN = "fixture-vin"
ENTRY_ID = "existing-entry"
ACCOUNT_ID = "fixture-account"
LEGACY_UNIQUE_ID = "legacy-fixture-id"

# Existing entity keys from fb87fcc. Keep these expectations independent of const.py.
BASELINE_KEYS = {
    "sensor": """
        battery_percentage remains_mileage fuel_percentage remains_mileage_fuel
        battery_voltage odometer battery_temp coolant_temp inboard_temp outside_temp
        climate_current_temp climate_target_temp climate_fan_speed latitude longitude
        altitude satellites hdop course last_online telemetry_time status_text
        online_state signal_level position_time firmware_version firmware_expected
        warnings_count prep_end_time prep_start_time prep_script_time maintenance_status
        maintenance_days_left maintenance_km_left maintenance_date maintenance_mileage
        last_sensor_request last_command_trigger
    """.split(),
    "binary_sensor": """
        ignition central_lock door_fl door_fr door_rl door_rr trunk headlights climate
        climate_front_defrost online moving preparing heating cooling prep_available
        prep_disabled prep_error warnings charging_gun charging car_state_ready
        firmware_mismatch settings_mismatch update_available location_enabled
    """.split(),
    "button": """
        central_lock_toggle heating_toggle cooling_toggle trunk_toggle blink
        prepare_start prepare_cancel
    """.split(),
    "device_tracker": ["tracker"],
}


class IdentifierCompatibilityTests(IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.integration = load_evolute_module("__init__")
        cls.platforms = {
            domain: load_evolute_module(domain) for domain in BASELINE_KEYS
        }

    async def load_existing_entry(self, unique_id, *, rotate=False):
        tokens = {
            "access_token": fake_access_jwt({"_id": ACCOUNT_ID, "iat": 1}),
            "refresh_token": "test-refresh",
            "extra_field": "keep-me",
        }
        entry = SimpleNamespace(entry_id=ENTRY_ID, unique_id=unique_id, data=tokens)
        session = Mock()
        replies = [
            response(200, {"rows": [{"_id": CAR_ID, "vin": VIN, "carModel": {"name": "Fixture"}}]}),
            response(200, {}),
            response(200, {"imei": "fixture-imei"}),
        ]
        rotated_access = fake_access_jwt({"userId": ACCOUNT_ID, "iat": 2})
        if rotate:
            replies.insert(0, response(401))
            session.post.return_value = response(200, {"accessToken": rotated_access})
        session.request.side_effect = replies
        entities = {}

        async def forward(forwarded_entry, platforms):
            self.assertIs(forwarded_entry, entry)
            self.assertCountEqual(platforms, self.platforms)
            for domain in platforms:
                await self.platforms[domain].async_setup_entry(
                    hass, forwarded_entry, entities.setdefault(domain, []).extend
                )

        config_entries = SimpleNamespace(
            async_forward_entry_setups=AsyncMock(side_effect=forward),
            async_update_entry=Mock(),
        )
        hass = SimpleNamespace(session=session, data={}, config_entries=config_entries)
        with (
            patch.object(self.integration, "_setup_www_files", new_callable=AsyncMock),
            patch.object(self.integration, "_register_card_resource", new_callable=AsyncMock),
            patch.object(
                self.integration.EvoluteDataUpdateCoordinator,
                "async_config_entry_first_refresh",
                new_callable=AsyncMock,
                create=True,
            ),
        ):
            self.assertTrue(await self.integration.async_setup_entry(hass, entry))

        self.assertEqual(entry.entry_id, ENTRY_ID)
        self.assertEqual(entry.unique_id, unique_id)
        self.assertEqual(set(hass.data["evolute"]), {ENTRY_ID})
        config_entries.async_forward_entry_setups.assert_awaited_once()
        if rotate:
            config_entries.async_update_entry.assert_called_once_with(
                entry, data={**tokens, "access_token": rotated_access}
            )
        else:
            config_entries.async_update_entry.assert_not_called()
            session.post.assert_not_called()
        return entities

    async def test_in_place_setup_preserves_baseline_registry_keys_and_default_ids(self):
        for entry_unique_id in (LEGACY_UNIQUE_ID, ACCOUNT_ID, None):
            with self.subTest(entry_unique_id=entry_unique_id):
                entities = await self.load_existing_entry(entry_unique_id)
                for domain, keys in BASELINE_KEYS.items():
                    defaults = {entity.unique_id: entity.entity_id for entity in entities[domain]}
                    self.assertEqual(len(defaults), len(entities[domain]))
                    for key in keys:
                        unique_id = f"evolute_{CAR_ID}_{key}"
                        object_id = f"evolute_{CAR_ID}" if domain == "device_tracker" else unique_id
                        self.assertEqual(defaults[unique_id], f"{domain}.{object_id}")
                    for entity in entities[domain]:
                        self.assertEqual(entity.device_info["identifiers"], {("evolute", CAR_ID)})
                        self.assertEqual(entity.device_info["serial_number"], VIN)

    async def test_existing_registry_resolves_default_and_renamed_ids_after_refresh(self):
        # Model the documented HA registry lookup for two pre-existing entities.
        saved_ids = {
            ("binary_sensor", "evolute", f"evolute_{CAR_ID}_charging"):
                f"binary_sensor.evolute_{CAR_ID}_charging",
            ("sensor", "evolute", f"evolute_{CAR_ID}_battery_percentage"):
                "sensor.garage_evolute_soc",
        }
        entities = await self.load_existing_entry(LEGACY_UNIQUE_ID, rotate=True)

        matches = {}
        for domain in ("binary_sensor", "sensor"):
            for entity in entities[domain]:
                registry_key = (domain, "evolute", entity.unique_id)
                if registry_key in saved_ids:
                    matches[entity.entity_id] = saved_ids[registry_key]

        self.assertEqual(matches, {
            f"binary_sensor.evolute_{CAR_ID}_charging": f"binary_sensor.evolute_{CAR_ID}_charging",
            f"sensor.evolute_{CAR_ID}_battery_percentage": "sensor.garage_evolute_soc",
        })
