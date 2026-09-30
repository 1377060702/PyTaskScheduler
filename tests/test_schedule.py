import copy
import importlib.util
import random
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "PyTaskScheduler.py"
SPEC = importlib.util.spec_from_file_location("schedule_under_test", MODULE_PATH)
scheduler = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scheduler)


def dt(value):
    return datetime.fromisoformat(value)


def task(**changes):
    value = dict(scheduler.TASK_DEFAULTS)
    value.update(id="test-task", start_date="2026-09-23", trigger_time="08:50:00")
    value.update(changes)
    return value


class ScheduleCalculationTests(unittest.TestCase):
    def test_legacy_daily_without_new_fields_keeps_daily_schedule(self):
        value = {"trigger_type": "daily", "start_date": "2026-09-23", "trigger_time": "08:50:00"}
        self.assertEqual(scheduler.compute_next_run(value, dt("2026-09-23 09:00:00")),
                         dt("2026-09-24 08:50:00"))

    def test_daily_defaults_remain_once_per_day(self):
        value = task()
        self.assertEqual(scheduler.compute_next_run(value, dt("2026-09-23 08:50:00")),
                         dt("2026-09-23 08:50:00"))
        self.assertEqual(scheduler.compute_next_run(value, dt("2026-09-23 08:50:00.000001")),
                         dt("2026-09-24 08:50:00"))

    def test_daily_repeat_uses_original_start_grid(self):
        value = task(daily_repeat_every=900, daily_repeat_duration=3600)
        for current, expected in (("08:50:00", "08:50:00"), ("08:50:01", "09:05:00"),
                                  ("09:05:00", "09:05:00"), ("09:06:00", "09:20:00")):
            with self.subTest(current=current):
                self.assertEqual(scheduler.compute_next_run(value, dt("2026-09-23 " + current)),
                                 dt("2026-09-23 " + expected))

    def test_daily_window_end_grid_is_included(self):
        value = task(daily_repeat_every=900, daily_repeat_duration=3600)
        self.assertEqual(scheduler.compute_next_run(value, dt("2026-09-23 09:50:00")),
                         dt("2026-09-23 09:50:00"))
        self.assertEqual(scheduler.compute_next_run(value, dt("2026-09-23 09:50:00.000001")),
                         dt("2026-09-24 08:50:00"))

    def test_daily_nondivisible_duration_excludes_later_grid(self):
        value = task(daily_repeat_every=900, daily_repeat_duration=2000)
        self.assertEqual(scheduler.compute_next_run(value, dt("2026-09-23 09:20:01")),
                         dt("2026-09-24 08:50:00"))

    def test_equal_every_and_duration_runs_start_and_end(self):
        value = task(daily_repeat_every=600, daily_repeat_duration=600)
        self.assertEqual(scheduler.compute_next_run(value, dt("2026-09-23 08:50:01")),
                         dt("2026-09-23 09:00:00"))

    def test_cross_midnight_window_with_every_three_days(self):
        value = task(trigger_time="23:00:00", every_n_days=3,
                     daily_repeat_every=1800, daily_repeat_duration=7200)
        self.assertEqual(scheduler.compute_next_run(value, dt("2026-09-24 00:01:00")),
                         dt("2026-09-24 00:30:00"))
        self.assertEqual(scheduler.compute_next_run(value, dt("2026-09-24 01:00:00")),
                         dt("2026-09-24 01:00:00"))
        self.assertEqual(scheduler.compute_next_run(value, dt("2026-09-24 01:00:00.000001")),
                         dt("2026-09-26 23:00:00"))

    def test_full_cycle_duration_starts_next_window_at_boundary(self):
        value = task(every_n_days=2, daily_repeat_every=3600, daily_repeat_duration=172800)
        self.assertEqual(scheduler.compute_next_run(value, dt("2026-09-25 08:50:00")),
                         dt("2026-09-25 08:50:00"))

    def test_window_can_span_multiple_midnights(self):
        value = task(every_n_days=3, daily_repeat_every=3600, daily_repeat_duration=172800)
        self.assertEqual(scheduler.compute_next_run(value, dt("2026-09-25 07:00:00")),
                         dt("2026-09-25 07:50:00"))
        self.assertEqual(scheduler.compute_next_run(value, dt("2026-09-25 08:50:01")),
                         dt("2026-09-26 08:50:00"))

    def test_disabled_repeat_ignores_old_duration(self):
        value = task(daily_repeat_every=0, daily_repeat_duration=999999)
        self.assertEqual(scheduler.compute_next_run(value, dt("2026-09-23 09:00:00")),
                         dt("2026-09-24 08:50:00"))

    def test_daily_grid_matches_enumerated_windows(self):
        anchor = dt("2026-09-23 23:00:00")
        for days, every, duration in ((1, 900, 3600), (2, 1800, 7200),
                                     (2, 70000, 172800), (3, 3600, 172800)):
            value = task(trigger_time="23:00:00", every_n_days=days,
                         daily_repeat_every=every, daily_repeat_duration=duration)
            grid = sorted({anchor + timedelta(days=cycle * days, seconds=offset)
                           for cycle in range(4) for offset in range(0, duration + 1, every)})
            probes = [anchor - timedelta(seconds=1)]
            for planned in grid[:-1]:
                probes.extend((planned, planned + timedelta(microseconds=1)))
            for current in probes:
                expected = next((planned for planned in grid if planned >= current), None)
                if expected is not None:
                    with self.subTest(days=days, every=every, duration=duration, current=current):
                        self.assertEqual(scheduler.compute_next_run(value, current), expected)

    def test_future_anchor_does_not_create_earlier_window(self):
        value = task(start_date="2026-09-26", daily_repeat_every=900, daily_repeat_duration=3600)
        self.assertEqual(scheduler.compute_next_run(value, dt("2026-09-23 09:00:00")),
                         dt("2026-09-26 08:50:00"))

    def test_invalid_repeat_parameters_do_not_schedule(self):
        for every, duration in ((900, 0), (900, 600), (900, 86401), (-1, 3600),
                                (None, 3600), ("", 3600), ("bad", 3600),
                                (1.5, 3600), (True, 3600), (900, 3600.5), (900, None)):
            with self.subTest(every=every, duration=duration):
                self.assertIsNone(scheduler.compute_next_run(
                    task(daily_repeat_every=every, daily_repeat_duration=duration),
                    dt("2026-09-23 08:00:00")))

    def test_daily_randomized_grid_matches_enumerated_windows(self):
        rng = random.Random(20260923)
        anchor = dt("2026-09-23 23:47:00")
        for _ in range(40):
            days = rng.randint(1, 7)
            every = rng.randint(600, days * 86400)
            duration = rng.randint(every, days * 86400)
            value = task(trigger_time="23:47:00", every_n_days=days,
                         daily_repeat_every=every, daily_repeat_duration=duration)
            grid = sorted({anchor + timedelta(days=cycle * days, seconds=offset)
                           for cycle in range(4) for offset in range(0, duration + 1, every)})
            for planned in grid[:-1]:
                for delta in (-1, 0, 1):
                    current = planned + timedelta(microseconds=delta)
                    expected = next(item for item in grid if item >= current)
                    with self.subTest(days=days, every=every, duration=duration, current=current):
                        self.assertEqual(scheduler.compute_next_run(value, current), expected)

    def test_interval_grid_includes_start_and_exact_boundary(self):
        value = task(trigger_type="interval", interval_start="2026-09-23 08:50:00",
                     interval_every=300, interval_duration=3600)
        for current, expected in (("08:50:00", "08:50:00"), ("08:55:00", "08:55:00"),
                                  ("08:55:00.000001", "09:00:00")):
            with self.subTest(current=current):
                self.assertEqual(scheduler.compute_next_run(value, dt("2026-09-23 " + current)),
                                 dt("2026-09-23 " + expected))

    def test_interval_window_end_grid_is_included(self):
        value = task(trigger_type="interval", interval_start="2026-09-23 08:50:00",
                     interval_every=300, interval_duration=3600)
        self.assertEqual(scheduler.compute_next_run(value, dt("2026-09-23 09:50:00")),
                         dt("2026-09-23 09:50:00"))
        self.assertIsNone(scheduler.compute_next_run(value, dt("2026-09-23 09:50:00.000001")))

    def test_unlimited_interval_keeps_running(self):
        value = task(trigger_type="interval", interval_start="2026-09-23 08:50:00",
                     interval_every=300, interval_duration=0)
        self.assertEqual(scheduler.compute_next_run(value, dt("2026-10-01 08:50:01")),
                         dt("2026-10-01 08:55:00"))
        self.assertIsNone(scheduler.repetition_window_end(value, dt("2026-10-01 08:55:00")))

    def test_once_calculation_never_returns_past(self):
        value = task(trigger_type="once", once_datetime="2026-09-23 08:50:00")
        self.assertIsNone(scheduler.compute_next_run(value, dt("2026-09-23 08:50:01")))

    def test_repetition_window_end_maps_cross_midnight_plan(self):
        helper = getattr(scheduler, "repetition_window_end", None)
        self.assertTrue(callable(helper))
        value = task(trigger_time="23:00:00", every_n_days=3,
                     daily_repeat_every=1800, daily_repeat_duration=7200)
        self.assertEqual(helper(value, dt("2026-09-24 00:30:00")), dt("2026-09-24 01:00:00"))
        self.assertIsNone(helper(task(), dt("2026-09-23 08:50:00")))
        self.assertEqual(helper(task(trigger_type="interval", interval_start="2026-09-23 08:00:00",
                                     interval_duration=3600), dt("2026-09-23 08:30:00")),
                         dt("2026-09-23 09:00:00"))

    def test_repeat_description_includes_interval_and_duration(self):
        description = scheduler.trigger_desc(task(daily_repeat_every=900, daily_repeat_duration=3600))
        self.assertIn("15 分钟", description)
        self.assertIn("1 小时", description)

    def test_endpoint_expiry_tolerates_only_endpoint_jitter(self):
        helper = getattr(scheduler, "repetition_window_expired", None)
        self.assertTrue(callable(helper))
        value = task(daily_repeat_every=900, daily_repeat_duration=3600)
        end = dt("2026-09-23 09:50:00")
        self.assertFalse(helper(value, end, end + timedelta(milliseconds=10)))
        self.assertFalse(helper(value, end, end + timedelta(seconds=1)))
        self.assertTrue(helper(value, end, end + timedelta(seconds=1, microseconds=1)))
        self.assertTrue(helper(value, end - timedelta(seconds=900), end + timedelta(milliseconds=10)))


class ScheduleRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.saved = []
        self.current = dt("2026-09-23 09:00:00")
        owner = self

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return owner.current

        self.patches = [mock.patch.object(scheduler, "datetime", Clock),
                        mock.patch.object(scheduler, "CONFIG_PATH", str(Path(self.temp.name) / "config.json")),
                        mock.patch.object(scheduler, "ERROR_LOG_PATH", str(Path(self.temp.name) / "errors.log")),
                        mock.patch.object(scheduler, "save_config", side_effect=self.persist),
                        mock.patch.object(scheduler, "_exhook")]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def persist(self, cfg):
        self.saved.append(copy.deepcopy(cfg))
        return True

    def make_scheduler(self, value, policy="run_once"):
        result = scheduler.Scheduler({"settings": {"missed_policy": policy, "missed_grace_seconds": 300},
                                      "tasks": [value]})
        result._fire_async = mock.Mock()
        return result

    def tick(self, instance):
        with mock.patch.object(scheduler.time, "time", return_value=self.current.timestamp()):
            instance._tick()

    def test_normalize_blank_daily_anchor_prefers_next_run(self):
        value = task(start_date=None, every_n_days=3, next_run="2026-09-25 08:50:00",
                     last_run="2026-09-21 08:50:00")
        scheduler.normalize_task(value)
        self.assertEqual(value["start_date"], "2026-09-25")

    def test_normalize_blank_daily_anchor_uses_last_run_then_today(self):
        for last, expected in (("2026-09-21 08:50:00", "2026-09-21"), (None, "2026-09-23")):
            value = task(start_date=None, last_run=last)
            scheduler.normalize_task(value)
            self.assertEqual(value["start_date"], expected)

    def test_cross_midnight_anchor_migration_uses_window_start_day(self):
        value = task(start_date=None, trigger_time="23:00:00", every_n_days=3,
                     daily_repeat_every=1800, daily_repeat_duration=7200,
                     next_run="2026-09-24 00:30:00")
        scheduler.normalize_task(value)
        self.assertEqual(value["start_date"], "2026-09-23")

    def test_interval_blank_anchor_is_stable(self):
        value = task(trigger_type="interval", interval_start=None, next_run="2026-09-23 08:55:00")
        scheduler.normalize_task(value)
        self.assertEqual(value["interval_start"], "2026-09-23 08:55:00")
        self.current += timedelta(days=1)
        scheduler.normalize_task(value)
        self.assertEqual(value["interval_start"], "2026-09-23 08:55:00")

    def test_restart_preserves_overdue_grid_for_policy(self):
        value = task(daily_repeat_every=900, daily_repeat_duration=3600,
                     next_run="2026-09-23 08:50:00")
        self.make_scheduler(value)
        self.assertEqual(value["next_run"], "2026-09-23 08:50:00")

    def test_restart_recalculates_invalid_grid(self):
        value = task(daily_repeat_every=900, daily_repeat_duration=3600,
                     next_run="2026-09-23 08:57:00")
        self.make_scheduler(value)
        self.assertEqual(value["next_run"], "2026-09-23 09:05:00")

    def test_restart_preserves_future_grid(self):
        value = task(daily_repeat_every=900, daily_repeat_duration=3600,
                     next_run="2026-09-23 09:20:00")
        self.make_scheduler(value)
        self.assertEqual(value["next_run"], "2026-09-23 09:20:00")

    def test_tick_at_exact_grid_advances_and_does_not_repeat(self):
        self.current = dt("2026-09-23 08:50:00")
        value = task(daily_repeat_every=900, daily_repeat_duration=3600,
                     next_run="2026-09-23 08:50:00")
        instance = self.make_scheduler(value)
        self.tick(instance)
        self.tick(instance)
        instance._fire_async.assert_called_once()
        self.assertEqual(value["next_run"], "2026-09-23 09:05:00")

    def test_late_within_grace_runs_even_when_missed_policy_is_skip(self):
        self.current = dt("2026-09-23 08:55:00")
        value = task(next_run="2026-09-23 08:50:00")
        instance = self.make_scheduler(value, policy="skip")
        self.tick(instance)
        instance._fire_async.assert_called_once_with(value, dt("2026-09-23 08:50:00"), missed=False)

    def test_late_run_once_emits_one_and_jumps_to_future_grid(self):
        value = task(daily_repeat_every=60, daily_repeat_duration=3600,
                     next_run="2026-09-23 08:50:00")
        instance = self.make_scheduler(value)
        self.tick(instance)
        self.tick(instance)
        instance._fire_async.assert_called_once_with(value, dt("2026-09-23 08:50:00"), missed=True)
        self.assertEqual(value["next_run"], "2026-09-23 09:01:00")

    def test_late_skip_does_not_emit(self):
        value = task(daily_repeat_every=60, daily_repeat_duration=3600,
                     next_run="2026-09-23 08:50:00")
        instance = self.make_scheduler(value, policy="skip")
        self.tick(instance)
        instance._fire_async.assert_not_called()
        self.assertEqual(value["next_run"], "2026-09-23 09:01:00")

    def test_expired_daily_window_never_catches_up_even_inside_grace(self):
        self.current = dt("2026-09-23 09:50:00.000001")
        value = task(daily_repeat_every=300, daily_repeat_duration=3600,
                     next_run="2026-09-23 09:45:00")
        instance = self.make_scheduler(value)
        self.tick(instance)
        instance._fire_async.assert_not_called()
        self.assertEqual(value["next_run"], "2026-09-24 08:50:00")

    def test_expired_interval_never_catches_up(self):
        self.current = dt("2026-09-23 09:00:00.000001")
        value = task(trigger_type="interval", interval_start="2026-09-23 08:00:00",
                     interval_every=300, interval_duration=3600, next_run="2026-09-23 08:55:00")
        instance = self.make_scheduler(value)
        self.tick(instance)
        instance._fire_async.assert_not_called()
        self.assertTrue(value["finished"])
        self.assertIsNone(value["next_run"])

    def test_endpoint_grid_tolerates_normal_poll_jitter(self):
        self.current = dt("2026-09-23 09:50:00.010000")
        value = task(daily_repeat_every=300, daily_repeat_duration=3600,
                     next_run="2026-09-23 09:50:00")
        instance = self.make_scheduler(value)
        self.tick(instance)
        instance._fire_async.assert_called_once()
        self.assertEqual(value["next_run"], "2026-09-24 08:50:00")

    def test_endpoint_grid_expired_after_sleep_is_not_caught_up(self):
        self.current = dt("2026-09-23 09:50:02")
        value = task(daily_repeat_every=300, daily_repeat_duration=3600,
                     next_run="2026-09-23 09:50:00")
        instance = self.make_scheduler(value)
        self.tick(instance)
        instance._fire_async.assert_not_called()

    def test_adjacent_windows_share_only_one_boundary_execution(self):
        self.current = dt("2026-09-24 08:50:00")
        value = task(daily_repeat_every=3600, daily_repeat_duration=86400,
                     next_run="2026-09-24 08:50:00")
        instance = self.make_scheduler(value)
        self.tick(instance)
        self.tick(instance)
        instance._fire_async.assert_called_once()
        self.assertEqual(value["next_run"], "2026-09-24 09:50:00")

    def test_finite_interval_emits_end_grid_then_finishes(self):
        value = task(trigger_type="interval", interval_start="2026-09-23 08:00:00",
                     interval_every=300, interval_duration=3600, next_run="2026-09-23 09:00:00")
        instance = self.make_scheduler(value)
        self.tick(instance)
        instance._fire_async.assert_called_once()
        self.assertTrue(value["finished"])

    def test_finite_interval_endpoint_tolerates_poll_jitter_and_stays_finished(self):
        self.current = dt("2026-09-23 09:00:00.010000")
        value = task(trigger_type="interval", interval_start="2026-09-23 08:00:00",
                     interval_every=300, interval_duration=3600, next_run="2026-09-23 09:00:00")
        instance = self.make_scheduler(value)
        self.tick(instance)
        instance._fire_async.assert_called_once()
        self.assertTrue(value["finished"])
        restored = self.make_scheduler(copy.deepcopy(self.saved[-1]["tasks"][0]))
        self.tick(restored)
        restored._fire_async.assert_not_called()

    def test_cross_midnight_window_restart_preserves_expired_plan_until_tick(self):
        self.current = dt("2026-09-25 23:47:00.000001")
        value = task(trigger_time="23:47:00", every_n_days=3,
                     daily_repeat_every=3600, daily_repeat_duration=172800,
                     next_run="2026-09-25 22:47:00")
        instance = self.make_scheduler(value)
        self.assertEqual(value["next_run"], "2026-09-25 22:47:00")
        self.tick(instance)
        instance._fire_async.assert_not_called()
        self.assertEqual(value["next_run"], "2026-09-26 23:47:00")

    def test_datetime_before_grid_does_not_fire_if_epoch_has_crossed_boundary(self):
        self.current = dt("2026-09-23 08:49:59.999999")
        value = task(next_run="2026-09-23 08:50:00")
        instance = self.make_scheduler(value)
        with mock.patch.object(scheduler.time, "time", return_value=self.current.timestamp() + 1):
            nearest = instance._tick()
        instance._fire_async.assert_not_called()
        self.assertEqual(value["next_run"], "2026-09-23 08:50:00")
        self.assertEqual(nearest, dt("2026-09-23 08:50:00").timestamp())

    def test_datetime_due_fires_once_if_epoch_is_behind(self):
        self.current = dt("2026-09-23 08:50:00")
        value = task(daily_repeat_every=300, daily_repeat_duration=3600,
                     next_run="2026-09-23 08:50:00")
        instance = self.make_scheduler(value)
        with mock.patch.object(scheduler.time, "time", return_value=self.current.timestamp() - 1):
            instance._tick()
            instance._tick()
        instance._fire_async.assert_called_once()
        self.assertEqual(value["next_run"], "2026-09-23 08:55:00")

    def test_closing_freezes_schedule_and_resume_consumes_it_once(self):
        value = task(daily_repeat_every=60, daily_repeat_duration=3600,
                     next_run="2026-09-23 08:50:00")
        instance = self.make_scheduler(value)
        before = copy.deepcopy(value)
        save_count = len(self.saved)
        instance._closing = True
        self.tick(instance)
        self.assertEqual(value, before)
        self.assertEqual(len(self.saved), save_count)
        instance._fire_async.assert_not_called()
        instance._closing = False
        self.tick(instance)
        instance._fire_async.assert_called_once()
        self.assertEqual(value["next_run"], "2026-09-23 09:01:00")

    def test_stop_during_tick_snapshot_does_not_consume_schedule(self):
        value = task(next_run="2026-09-23 08:50:00")
        instance = self.make_scheduler(value)
        before = copy.deepcopy(value)
        instance.stop_event.set()
        instance._tick_one(value, self.current, self.current.timestamp(), 300, "run_once", None)
        instance._fire_async.assert_not_called()
        self.assertEqual(value, before)

    def test_disabled_task_is_never_emitted(self):
        value = task(next_run="2026-09-23 08:50:00", enabled=False)
        instance = self.make_scheduler(value)
        self.tick(instance)
        instance._fire_async.assert_not_called()

    def test_once_without_saved_next_run_still_uses_grace(self):
        self.current = dt("2026-09-23 08:53:00")
        value = task(trigger_type="once", once_datetime="2026-09-23 08:50:00", next_run=None)
        instance = self.make_scheduler(value)
        self.tick(instance)
        instance._fire_async.assert_called_once()
        self.assertTrue(value["finished"])

    def test_once_outside_grace_finishes_without_emitting(self):
        value = task(trigger_type="once", once_datetime="2026-09-23 08:50:00",
                     next_run="2026-09-23 08:50:00")
        instance = self.make_scheduler(value)
        self.tick(instance)
        instance._fire_async.assert_not_called()
        self.assertTrue(value["finished"])

    def test_consumed_schedule_is_persisted_before_emit_and_survives_restart(self):
        value = task(daily_repeat_every=60, daily_repeat_duration=3600,
                     next_run="2026-09-23 08:50:00")
        instance = self.make_scheduler(value)
        instance._fire_async.side_effect = lambda *args, **kwargs: self.assertEqual(
            self.saved[-1]["tasks"][0]["next_run"], "2026-09-23 09:01:00")
        self.tick(instance)
        self.assertTrue(self.saved)
        for _ in range(3):
            restored = self.make_scheduler(copy.deepcopy(self.saved[-1]["tasks"][0]))
            self.tick(restored)
            restored._fire_async.assert_not_called()

    def test_persist_failure_keeps_schedule_and_does_not_emit(self):
        value = task(daily_repeat_every=60, daily_repeat_duration=3600,
                     next_run="2026-09-23 08:50:00")
        instance = self.make_scheduler(value)
        with mock.patch.object(scheduler, "save_config", return_value=False):
            self.tick(instance)
        instance._fire_async.assert_not_called()
        self.assertEqual(value["next_run"], "2026-09-23 08:50:00")
        self.assertFalse(value["finished"])

    def test_final_plan_persist_failure_rolls_back_then_retries_once(self):
        for trigger in ("once", "interval"):
            with self.subTest(trigger=trigger):
                value = task(trigger_type=trigger, once_datetime="2026-09-23 09:00:00",
                             interval_start="2026-09-23 08:00:00", interval_every=300,
                             interval_duration=3600, next_run="2026-09-23 09:00:00")
                instance = self.make_scheduler(value)
                with mock.patch.object(scheduler, "save_config", return_value=False):
                    self.tick(instance)
                instance._fire_async.assert_not_called()
                self.assertEqual(value["next_run"], "2026-09-23 09:00:00")
                self.assertFalse(value["finished"])
                self.tick(instance)
                self.tick(instance)
                instance._fire_async.assert_called_once()
                self.assertTrue(value["finished"])
                self.assertIsNone(value["next_run"])

    def test_blank_anchor_is_persisted_and_stays_stable_across_restarts(self):
        value = task(start_date=None, every_n_days=3, next_run=None)
        self.make_scheduler(value)
        self.assertEqual(self.saved[-1]["tasks"][0]["start_date"], "2026-09-23")
        self.assertEqual(value["next_run"], "2026-09-26 08:50:00")
        for day in (24, 25):
            self.current = dt("2026-09-%d 09:00:00" % day)
            restored = copy.deepcopy(self.saved[-1]["tasks"][0])
            self.make_scheduler(restored)
            self.assertEqual(restored["start_date"], "2026-09-23")
            self.assertEqual(restored["next_run"], "2026-09-26 08:50:00")

    def test_stale_task_reference_cannot_override_replacement(self):
        value = task(next_run="2026-09-23 08:50:00")
        instance = self.make_scheduler(value)
        replacement = task(next_run="2026-09-24 08:50:00")
        instance.cfg["tasks"] = [replacement]
        instance._tick_one(value, self.current, self.current.timestamp(), 300, "run_once", None)
        instance._fire_async.assert_not_called()
        self.assertEqual(replacement["next_run"], "2026-09-24 08:50:00")


if __name__ == "__main__":
    unittest.main()
