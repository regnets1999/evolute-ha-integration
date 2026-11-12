"""Offline vehicle identity regressions through Evolute and its four platforms."""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

from test_config_flow import ConfigEntryNotReady, fake_access_jwt, load_evolute_module, response


CAR_ID = "c0ffee000000000000000001"
SECOND_CAR_ID = "c0ffee000000000000000002"
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
        cls.identity = load_evolute_module("vehicle_identity")
        cls.config_flow = load_evolute_module("config_flow")
        cls.platforms = {
            domain: load_evolute_module(domain) for domain in BASELINE_KEYS
        }

    def setUp(self):
        directory = TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(directory.cleanup)
        self.storage_dir = Path(directory.name)
        self.store_path = self.storage_dir / self.identity.STORAGE_KEY
        self.entry = SimpleNamespace(
            entry_id=ENTRY_ID, unique_id=LEGACY_UNIQUE_ID,
            data={
                "access_token": fake_access_jwt({"_id": ACCOUNT_ID, "iat": 1}),
                "refresh_token": "test-refresh",
                "extra_field": "keep-me",
            },
        )

    def new_hass(self):
        async def run_job(target, *args):
            return target(*args)

        async def load_store(path):
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return None
            except json.JSONDecodeError:
                path.rename(path.with_name(f"{path.name}.corrupt.fixture"))
                return None

        async def save_store(path, data):
            path.write_text(json.dumps(data), encoding="utf-8")

        def update_entry(entry, *, data):
            entry.data = data

        return SimpleNamespace(
            session=Mock(), data={}, storage_dir=self.storage_dir,
            is_stopping=False,
            async_create_task=asyncio.create_task, async_add_executor_job=run_job,
            load_identity_store=AsyncMock(side_effect=load_store),
            save_identity_store=AsyncMock(side_effect=save_store),
            config_entries=SimpleNamespace(
                async_update_entry=Mock(side_effect=update_entry),
                async_get_entry=Mock(return_value=self.entry),
                async_unload_platforms=AsyncMock(return_value=True),
            ),
        )

    def car_replies(self, car_ids, *, model="Fixture"):
        rows = [
            {
                "_id": car_id, "vin": VIN,
                "carModel": {"name": model, "modname": VIN, "color": car_id},
                "images": {"side": f"https://example.invalid/{VIN.replace('v', '%76', 1)}"},
            }
            for car_id in car_ids
        ]
        return [response(200, {"rows": rows}), response(200, {})] + [
            response(200, {"imei": "fixture-imei"}) for _ in rows
        ]

    async def load_existing_entry(self, hass, car_ids=(CAR_ID,), *, model="Fixture"):
        hass.session.request.side_effect = self.car_replies(car_ids, model=model)
        entities = {}

        async def forward(forwarded_entry, platforms):
            self.assertIs(forwarded_entry, self.entry)
            self.assertCountEqual(platforms, self.platforms)
            for domain in platforms:
                await self.platforms[domain].async_setup_entry(
                    hass, forwarded_entry, entities.setdefault(domain, []).extend
                )

        hass.config_entries.async_forward_entry_setups = AsyncMock(side_effect=forward)
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
            self.assertTrue(await self.integration.async_setup_entry(hass, self.entry))

        self.assertEqual(self.entry.entry_id, ENTRY_ID)
        self.assertEqual(self.entry.unique_id, LEGACY_UNIQUE_ID)
        self.assertEqual(set(hass.data["evolute"]), {ENTRY_ID})
        hass.config_entries.async_forward_entry_setups.assert_awaited_once()
        return entities

    def tracker_identities(self, entities):
        return {
            tracker._car_id: (tracker.unique_id, tracker.entity_id, tracker._attr_name)
            for tracker in entities["device_tracker"]
        }

    async def test_all_platforms_use_local_uuids_and_keep_existing_entity_keys(self):
        hass = self.new_hass()
        entities = await self.load_existing_entry(hass, (CAR_ID, SECOND_CAR_ID))
        uuids = {}
        for tracker in entities["device_tracker"]:
            local_id = next(iter(tracker.device_info["identifiers"]))[1]
            self.assertEqual(UUID(local_id).version, 4)
            uuids[tracker._car_id] = local_id
            tracker.coordinator.data = {}
            self.assertNotIn("vin", tracker.extra_state_attributes)
            self.assertNotIn("color", tracker.extra_state_attributes)
            self.assertFalse(hasattr(tracker, "_attr_entity_picture"))
        self.assertEqual(len(set(uuids.values())), 2)
        self.assertEqual({tracker._attr_name for tracker in entities["device_tracker"]}, {"Fixture 1", "Fixture 2"})
        for domain, keys in BASELINE_KEYS.items():
            defaults = {entity.unique_id: entity.entity_id for entity in entities[domain]}
            self.assertEqual(len(defaults), len(entities[domain]))
            for local_id in uuids.values():
                for key in keys:
                    unique_id = f"evolute_{local_id}_{key}"
                    object_id = f"evolute_{local_id}" if domain == "device_tracker" else unique_id
                    self.assertEqual(defaults[unique_id], f"{domain}.{object_id}")
            for entity in entities[domain]:
                self.assertEqual(entity.device_info["identifiers"], {("evolute", uuids[entity._car_id])})
                self.assertNotIn("serial_number", entity.device_info)
                public = repr((entity.unique_id, entity.entity_id, entity._attr_name, entity.device_info))
                for private in (CAR_ID, SECOND_CAR_ID, VIN):
                    self.assertNotIn(private, public)
        request_urls = [call.args[1] for call in hass.session.request.call_args_list]
        for car_id in (CAR_ID, SECOND_CAR_ID):
            self.assertTrue(any(url.endswith(f"/car/v2/{car_id}") for url in request_urls))

    async def test_reload_restart_reordering_and_temporary_absence_keep_identities(self):
        hass = self.new_hass()
        original = self.tracker_identities(await self.load_existing_entry(hass, (SECOND_CAR_ID, CAR_ID)))
        self.assertEqual(original[CAR_ID][2], "Fixture 1")
        self.assertTrue(await self.integration.async_unload_entry(hass, self.entry))
        partial = self.tracker_identities(await self.load_existing_entry(hass, (SECOND_CAR_ID,)))
        self.assertEqual(partial, {SECOND_CAR_ID: original[SECOND_CAR_ID]})

        restarted = self.new_hass()
        both = self.tracker_identities(await self.load_existing_entry(restarted, (CAR_ID, SECOND_CAR_ID)))
        self.assertEqual(both, original)
        self.assertEqual(hass.save_identity_store.await_count, 1)
        restarted.save_identity_store.assert_not_awaited()

    async def test_reauth_reloads_same_entry_with_same_vehicle_identities(self):
        hass = self.new_hass()
        before = self.tracker_identities(await self.load_existing_entry(hass))
        after = {}

        async def reload_entry(entry_id):
            self.assertEqual(entry_id, ENTRY_ID)
            self.assertTrue(await self.integration.async_unload_entry(hass, self.entry))
            after.update(self.tracker_identities(await self.load_existing_entry(hass)))

        hass.config_entries.async_reload = AsyncMock(side_effect=reload_entry)
        hass.session.request.side_effect = self.car_replies((CAR_ID,))
        flow = self.config_flow.ConfigFlow()
        flow.hass = hass
        flow.context = {"entry_id": ENTRY_ID}
        flow.async_show_form = Mock()
        flow.async_abort = Mock()
        await flow.async_step_reauth(self.entry.data)
        updated = {**self.entry.data, "access_token": fake_access_jwt({"_id": ACCOUNT_ID, "iat": 2})}
        await flow.async_step_reauth_confirm(updated)

        flow.async_abort.assert_called_once_with(reason="reauth_successful")
        self.assertEqual(after, before)
        self.assertEqual(self.entry.data, updated)
        self.assertEqual(hass.save_identity_store.await_count, 1)

    async def test_setup_title_and_missing_model_do_not_expose_private_ids(self):
        hass = self.new_hass()
        for model, title in (("Fixture", "Evolute (Fixture)"), (f"{VIN} {CAR_ID}", "Evolute")):
            hass.session.request.side_effect = self.car_replies((CAR_ID,), model=model)
            info = await self.config_flow.validate_input(hass, self.entry.data)
            self.assertEqual(info["title"], title)
        entities = await self.load_existing_entry(hass, model=f"{VIN} {CAR_ID}")
        self.assertEqual(entities["device_tracker"][0]._attr_name, "Evolute 1")

    async def test_command_info_log_does_not_expose_server_id_or_vin(self):
        entities = await self.load_existing_entry(self.new_hass())
        button = next(entity for entity in entities["button"] if entity.unique_id.endswith("_blink"))
        button.coordinator.data = {}
        with (
            patch.object(button.coordinator, "async_send_command", new_callable=AsyncMock, return_value=True) as send,
            self.assertLogs(self.platforms["button"].__name__, level="INFO") as logs,
        ):
            await button.async_press()
        send.assert_awaited_once_with(CAR_ID, "blink", None)
        self.assertNotIn(CAR_ID, "\n".join(logs.output))
        self.assertNotIn(VIN, "\n".join(logs.output))

    async def check_parallel_allocations(self, *, cancel_first=False):
        hass = self.new_hass()
        save = hass.save_identity_store.side_effect
        started = asyncio.Event()
        finish = asyncio.Event()
        second_started = asyncio.Event()

        async def delayed_save(path, data):
            started.set()
            await finish.wait()
            await save(path, data)

        hass.save_identity_store.side_effect = delayed_save
        first = [{"car_id": CAR_ID, "model": "Fixture"}]
        second = [{"car_id": CAR_ID, "model": "Fixture"}]
        first_task = asyncio.create_task(self.identity.async_assign_vehicle_identities(hass, first))
        await asyncio.wait_for(started.wait(), timeout=2)

        async def observe_start(coro):
            second_started.set()
            return await coro

        hass.async_create_task = lambda coro: asyncio.create_task(observe_start(coro))
        try:
            if cancel_first:
                first_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first_task
            second_task = asyncio.create_task(self.identity.async_assign_vehicle_identities(hass, second))
            await asyncio.wait_for(second_started.wait(), timeout=2)
            hass.save_identity_store.assert_awaited_once()
        finally:
            finish.set()
        await second_task
        if cancel_first:
            self.assertNotIn("local_id", first[0])
            self.assertIn("local_id", second[0])
        else:
            await first_task
            self.assertEqual(first, second)
        hass.save_identity_store.assert_awaited_once()

    async def test_parallel_allocations_share_one_write(self):
        await self.check_parallel_allocations()

    async def test_cancellation_does_not_interrupt_an_in_progress_save(self):
        await self.check_parallel_allocations(cancel_first=True)

    async def test_corrupt_store_is_not_recreated_on_retry_or_restart(self):
        self.store_path.write_text("{", encoding="utf-8")
        hass = self.new_hass()
        with patch.object(self.identity, "uuid4") as allocate:
            for instance in (hass, hass, self.new_hass()):
                cars = [{"car_id": CAR_ID, "model": "Fixture"}]
                with self.assertRaisesRegex(ConfigEntryNotReady, "Could not read"):
                    await self.identity.async_assign_vehicle_identities(instance, cars)
                self.assertNotIn("local_id", cars[0])
                instance.save_identity_store.assert_not_awaited()
            allocate.assert_not_called()
        self.assertTrue(self.store_path.with_name(f"{self.store_path.name}.corrupt.fixture").is_file())

    async def test_load_error_and_invalid_store_never_allocate_uuid(self):
        self.store_path.write_text('{"invalid": {"uuid": "not-a-uuid"}}', encoding="utf-8")
        hass = self.new_hass()
        with patch.object(self.identity, "uuid4") as allocate:
            for error in (None, OSError("read failed")):
                if error:
                    hass.load_identity_store.side_effect = error
                with self.assertRaisesRegex(ConfigEntryNotReady, "Could not read"):
                    await self.identity.async_assign_vehicle_identities(hass, [{"car_id": CAR_ID}])
            allocate.assert_not_called()
        hass.save_identity_store.assert_not_awaited()

    async def test_write_failure_never_registers_unpersisted_identity(self):
        hass = self.new_hass()
        original = self.tracker_identities(await self.load_existing_entry(hass))
        saved = self.store_path.read_bytes()
        write = hass.save_identity_store.side_effect
        for failure in (OSError("write failed"), None):
            hass.save_identity_store.side_effect = failure
            with self.assertRaisesRegex(ConfigEntryNotReady, "Could not save"):
                await self.load_existing_entry(hass, (CAR_ID, SECOND_CAR_ID))
            hass.config_entries.async_forward_entry_setups.assert_not_awaited()
            self.assertEqual(self.store_path.read_bytes(), saved)

        hass.save_identity_store.side_effect = write
        recovered = self.tracker_identities(await self.load_existing_entry(hass, (CAR_ID, SECOND_CAR_ID)))
        self.assertEqual(recovered[CAR_ID], original[CAR_ID])
        self.assertNotEqual(recovered[CAR_ID], recovered[SECOND_CAR_ID])

    async def test_retry_reuses_written_uuid_after_verification_read_failure(self):
        hass = self.new_hass()
        load = hass.load_identity_store.side_effect

        async def fail_second_read(path):
            if hass.load_identity_store.await_count == 2:
                raise OSError("verification read failed")
            return await load(path)

        hass.load_identity_store.side_effect = fail_second_read
        with self.assertRaisesRegex(ConfigEntryNotReady, "Could not save"):
            await self.load_existing_entry(hass)
        hass.config_entries.async_forward_entry_setups.assert_not_awaited()
        saved_uuid = json.loads(self.store_path.read_text(encoding="utf-8"))[json.dumps(CAR_ID)]["uuid"]

        tracker = (await self.load_existing_entry(hass))["device_tracker"][0]

        self.assertEqual(tracker.device_info["identifiers"], {("evolute", saved_uuid)})
        hass.save_identity_store.assert_awaited_once()
