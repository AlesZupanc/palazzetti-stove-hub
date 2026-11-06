import datetime as dt
import unittest

from helpers import FakeStove, box_unreachable, make_config, temp_db

from stovemaster.scheduler import (Scheduler, active_at, days_to_mask, next_start, validate_entry)

MON = dt.date(2026, 10, 5)  # a Monday


def at(day_offset, hhmm):
    h, m = map(int, hhmm.split(":"))
    return dt.datetime.combine(MON + dt.timedelta(days=day_offset), dt.time(h, m))


def slot(db, name, days, start, end, power=3, fan=7, setpoint=21.0, enabled=True):
    entry = validate_entry({"name": name, "days": days, "start": start, "end": end, "power": power,
                            "fan": fan, "setpoint": setpoint, "enabled": enabled})
    return db.schedule_save(entry, by="test")


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class SlotMath(unittest.TestCase):
    def setUp(self):
        self.db = temp_db()

    def entries(self):
        return self.db.schedule_entries()

    def test_simple_and_days(self):
        slot(self.db, "morning", [0, 1, 2, 3, 4], "06:00", "08:00")
        self.assertEqual(active_at(self.entries(), at(0, "06:00")).name, "morning")
        self.assertEqual(active_at(self.entries(), at(0, "07:59")).name, "morning")
        self.assertIsNone(active_at(self.entries(), at(0, "08:00")))   # end is exclusive
        self.assertIsNone(active_at(self.entries(), at(0, "05:59")))
        self.assertIsNone(active_at(self.entries(), at(5, "07:00")))   # Saturday

    def test_overnight(self):
        slot(self.db, "night", ["Sat"], "22:00", "02:00")
        self.assertEqual(active_at(self.entries(), at(5, "23:30")).name, "night")
        self.assertEqual(active_at(self.entries(), at(6, "01:59")).name, "night")  # Sunday morning
        self.assertIsNone(active_at(self.entries(), at(6, "02:00")))
        self.assertIsNone(active_at(self.entries(), at(6, "23:00")))           # Sunday not in the days

    def test_overlap_latest_start_wins(self):
        slot(self.db, "day", ["Mon"], "06:00", "20:00", power=2)
        slot(self.db, "boost", ["Mon"], "07:00", "08:00", power=5)
        self.assertEqual(active_at(self.entries(), at(0, "06:30")).name, "day")
        self.assertEqual(active_at(self.entries(), at(0, "07:30")).name, "boost")
        self.assertEqual(active_at(self.entries(), at(0, "08:30")).name, "day")

    def test_disabled_and_next(self):
        slot(self.db, "off", ["Mon"], "06:00", "08:00", enabled=False)
        slot(self.db, "tue", ["Tue"], "09:00", "10:00")
        self.assertIsNone(active_at(self.entries(), at(0, "07:00")))
        nxt = next_start(self.entries(), at(0, "07:00"))
        self.assertEqual((nxt.name, nxt.start), ("tue", at(1, "09:00")))

    def test_ids_are_not_reused(self):
        first = slot(self.db, "a", ["Mon"], "06:00", "08:00")
        self.db.schedule_delete(first)
        self.assertNotEqual(slot(self.db, "b", ["Mon"], "06:00", "08:00"), first)

    def test_validation(self):
        bad = [
            {"days": [], "start": "06:00", "end": "07:00", "power": 3, "fan": 7, "setpoint": 21},
            {"days": [0], "start": "24:00", "end": "07:00", "power": 3, "fan": 7, "setpoint": 21},
            {"days": [0], "start": "06:00", "end": "06:00", "power": 3, "fan": 7, "setpoint": 21},
            {"days": [0], "start": "06:00", "end": "07:00", "power": 6, "fan": 7, "setpoint": 21},
            {"days": [0], "start": "06:00", "end": "07:00", "power": 3, "fan": 8, "setpoint": 21},
            {"days": [0], "start": "06:00", "end": "07:00", "power": 3, "fan": 7, "setpoint": 99},
            {"days": [7], "start": "06:00", "end": "07:00", "power": 3, "fan": 7, "setpoint": 21},
            {"days": [0], "start": "06:00", "end": "07:00", "power": 2.5, "fan": 7, "setpoint": 21},
        ]
        for data in bad:
            with self.assertRaises(ValueError, msg=data):
                validate_entry(data)
        self.assertEqual(days_to_mask(["mon", "Sunday"]), 0b1000001)


class SchedulerRuns(unittest.TestCase):
    def setUp(self):
        self.db = temp_db()
        self.stove = FakeStove(status=0)
        self.notes = []
        self.clock = Clock(at(0, "05:00"))
        self.cfg = make_config(scheduler_off_grace_minutes="120", scheduler_start_wait_minutes="30")
        self.sched = self.new_scheduler()

    def new_scheduler(self):
        return Scheduler(self.db, self.stove, self.cfg, notify=lambda s, b: self.notes.append(s), clock=self.clock)

    def tick(self, when):
        self.clock.now = when
        self.sched.tick(when)

    def cmds(self):
        out, self.stove.commands = self.stove.commands, []
        return out

    def test_start_and_end(self):
        slot(self.db, "morning", ["Mon"], "06:00", "08:00", power=4, fan=2, setpoint=22.5)
        self.tick(at(0, "05:59"))
        self.assertEqual(self.cmds(), [])
        self.tick(at(0, "06:00"))
        self.assertEqual(self.cmds(), [("on", None), ("setpoint", 22.5), ("power", 4), ("fan", 2)])
        self.tick(at(0, "07:00"))
        self.assertEqual(self.cmds(), [])      # nothing between boundaries
        self.tick(at(0, "08:00"))
        self.assertEqual(self.cmds(), [("off", None)])
        events = [e["detail"] for e in self.db.events()]
        self.assertTrue(any("switched on" in d for d in events))
        self.assertTrue(any("switched off" in d for d in events))

    def test_running_stove_only_gets_settings(self):
        self.stove.state.update(STATUS=6, SETP=21.0, PWR=3, F2L=7)
        slot(self.db, "s", ["Mon"], "06:00", "08:00", power=3, fan=7, setpoint=23)
        self.tick(at(0, "06:00"))
        self.assertEqual(self.cmds(), [("setpoint", 23.0)])   # power/fan already right

    def test_waits_for_cooldown(self):
        self.stove.state["STATUS"] = 12  # cooling
        slot(self.db, "s", ["Mon"], "06:00", "08:00")
        self.tick(at(0, "06:00"))
        self.assertEqual(self.cmds(), [])
        self.tick(at(0, "06:00").replace(second=30))
        self.assertEqual(self.cmds(), [])      # retry not due yet
        self.stove.state["STATUS"] = 0
        self.tick(at(0, "06:01").replace(second=1))
        self.assertEqual(self.cmds()[0], ("on", None))

    def test_gives_up_and_notifies(self):
        self.stove.fail = box_unreachable()
        slot(self.db, "s", ["Mon"], "06:00", "09:00")
        for minute in range(0, 40):
            self.tick(at(0, "06:00") + dt.timedelta(minutes=minute, seconds=5))
        self.assertEqual(self.sched.state["phase"], "gave_up")
        self.assertEqual(self.notes, ["Stove schedule: slot could not start"])

    def test_back_to_back_slots_do_not_switch_off(self):
        slot(self.db, "a", ["Mon"], "06:00", "08:00", power=2)
        slot(self.db, "b", ["Mon"], "08:00", "10:00", power=5)
        self.tick(at(0, "06:00"))
        self.cmds()
        self.tick(at(0, "08:00"))
        self.assertEqual(self.cmds(), [("power", 5)])
        self.tick(at(0, "10:00"))
        self.assertEqual(self.cmds(), [("off", None)])

    def test_manual_off_before_pending_start_wins(self):
        self.stove.state["STATUS"] = 12
        slot(self.db, "s", ["Mon"], "06:00", "08:00")
        self.tick(at(0, "06:00"))          # waiting for cool-down
        self.clock.now = at(0, "06:00").replace(second=20)
        self.sched.notify_manual("off")    # user switches off by hand
        self.stove.state["STATUS"] = 0
        self.tick(at(0, "06:02"))
        self.assertEqual(self.cmds(), [])   # the scheduler does not switch it back on
        self.assertEqual(self.sched.state["phase"], "overridden")

    def test_manual_off_before_slot_does_not_cancel_it(self):
        slot(self.db, "s", ["Mon"], "06:00", "08:00")
        self.clock.now = at(0, "05:59")
        self.sched.notify_manual("off")
        self.tick(at(0, "06:00"))
        self.assertEqual(self.cmds()[0], ("on", None))

    def test_manual_on_after_end_cancels_pending_off(self):
        slot(self.db, "s", ["Mon"], "06:00", "08:00")
        self.tick(at(0, "06:00"))
        self.cmds()
        self.stove.fail = box_unreachable()
        self.tick(at(0, "08:00"))          # off fails -> retry pending
        self.assertIsNotNone(self.sched.state["off"])
        self.stove.fail = None
        self.clock.now = at(0, "08:00").replace(second=30)
        self.sched.notify_manual("on")
        self.tick(at(0, "08:02"))
        self.assertEqual(self.cmds(), [])
        self.assertIsNone(self.sched.state["off"])

    def test_restart_mid_slot_does_not_restart_stove(self):
        slot(self.db, "s", ["Mon"], "06:00", "08:00")
        self.tick(at(0, "06:00"))
        self.cmds()
        self.stove.state["STATUS"] = 0      # user switched off on the stove itself
        self.sched = self.new_scheduler()   # hub restarts
        self.tick(at(0, "07:00"))
        self.assertEqual(self.cmds(), [])

    def test_slot_started_while_hub_was_down(self):
        slot(self.db, "s", ["Mon"], "06:00", "08:00")
        self.tick(at(0, "05:00"))
        self.sched = self.new_scheduler()
        self.tick(at(0, "07:30"))           # hub comes back mid-slot
        self.assertEqual(self.cmds()[0], ("on", None))

    def test_missed_end_within_grace(self):
        slot(self.db, "s", ["Mon"], "06:00", "08:00")
        self.tick(at(0, "06:00"))
        self.cmds()
        self.sched = self.new_scheduler()
        self.tick(at(0, "09:30"))           # 90 min late, grace is 120
        self.assertEqual(self.cmds(), [("off", None)])

    def test_missed_end_beyond_grace(self):
        slot(self.db, "s", ["Mon"], "06:00", "08:00")
        self.tick(at(0, "06:00"))
        self.cmds()
        self.sched = self.new_scheduler()
        self.tick(at(0, "13:00"))
        self.assertEqual(self.cmds(), [])

    def test_deleted_slot_leaves_stove_alone(self):
        sid = slot(self.db, "s", ["Mon"], "06:00", "08:00")
        self.tick(at(0, "06:00"))
        self.cmds()
        self.db.schedule_delete(sid)
        self.tick(at(0, "06:30"))
        self.tick(at(0, "08:00"))
        self.assertEqual(self.cmds(), [])

    def test_shortened_slot_ends_now(self):
        sid = slot(self.db, "s", ["Mon"], "06:00", "10:00")
        self.tick(at(0, "06:00"))
        self.cmds()
        entry = validate_entry({"name": "s", "days": ["Mon"], "start": "06:00", "end": "07:00",
                                "power": 3, "fan": 7, "setpoint": 21})
        self.db.schedule_save(entry, entry_id=sid)
        self.tick(at(0, "07:30"))
        self.assertEqual(self.cmds(), [("off", None)])

    def test_pause_keeps_stove_as_is(self):
        slot(self.db, "s", ["Mon"], "06:00", "08:00")
        self.tick(at(0, "06:00"))
        self.cmds()
        self.sched.set_enabled(False)
        self.tick(at(0, "06:10"))
        self.tick(at(0, "08:10"))
        self.assertEqual(self.cmds(), [])   # paused: no switch-off at the end either

    def test_resume_inside_slot_starts_it(self):
        slot(self.db, "s", ["Mon"], "06:00", "08:00")
        self.tick(at(0, "06:00"))
        self.cmds()
        self.sched.set_enabled(False)
        self.tick(at(0, "06:10"))
        self.stove.state["STATUS"] = 0
        self.sched.set_enabled(True)
        self.tick(at(0, "06:20"))
        self.assertEqual(self.cmds(), [("on", None)])
        self.tick(at(0, "08:00"))
        self.assertEqual(self.cmds(), [("off", None)])

    def test_alarm_blocks_start(self):
        self.stove.state["STATUS"] = 253
        slot(self.db, "s", ["Mon"], "06:00", "08:00")
        self.tick(at(0, "06:00"))
        self.assertEqual(self.cmds(), [])
        self.assertIn("ALARM", self.sched.state["error"])

    def test_info(self):
        slot(self.db, "s", ["Mon"], "06:00", "08:00")
        info = self.sched.info(at(0, "05:00"))
        self.assertIsNone(info["active"])
        self.assertEqual(info["next"]["start"], "06:00")
        self.tick(at(0, "06:00"))
        self.assertEqual(self.sched.info(at(0, "06:00"))["active"]["phase"], "pending")  # lighting
        self.tick(at(0, "06:01"))
        self.assertEqual(self.sched.info(at(0, "06:30"))["active"]["phase"], "done")

    # -- scenarios from the code review -------------------------------------------
    def events(self):
        return [e["detail"] for e in self.db.events(50)]

    def test_success_is_reported_only_once_the_stove_lights(self):
        slot(self.db, "s", ["Mon"], "06:00", "08:00")
        self.tick(at(0, "06:00"))
        self.assertFalse(any("switched on" in d for d in self.events()))
        self.tick(at(0, "06:01"))
        self.assertTrue(any("switched on" in d for d in self.events()))
        self.assertEqual(self.sched.state["phase"], "done")

    def test_ignored_on_is_resent_then_reported(self):
        self.stove.on_turns = 0                     # ON is accepted but nothing happens
        slot(self.db, "s", ["Mon"], "06:00", "08:00")
        for minute in range(5):
            self.tick(at(0, "06:00") + dt.timedelta(minutes=minute, seconds=1))
        self.assertEqual([c for c in self.cmds() if c[0] == "on"], [("on", None)] * 3)
        self.assertEqual(self.sched.state["phase"], "gave_up")
        self.assertEqual(self.notes, ["Stove schedule: the stove did not start"])
        self.assertFalse(any("switched on" in d for d in self.events()))

    def test_failed_setting_after_on_does_not_claim_success(self):
        self.stove.on_turns = 0
        self.stove.fail_once = {"setpoint"}
        slot(self.db, "s", ["Mon"], "06:00", "08:00", setpoint=23)
        self.tick(at(0, "06:00"))                   # ON ok, SET SETP times out
        self.tick(at(0, "06:01").replace(second=5))  # still off: ON again, not "done"
        self.assertEqual([c[0] for c in self.cmds()], ["on", "on", "setpoint"])
        self.assertEqual(self.sched.state["phase"], "pending")

    def test_switched_off_on_the_stove_after_lighting_is_respected(self):
        slot(self.db, "s", ["Mon"], "06:00", "08:00")
        self.tick(at(0, "06:00"))
        self.cmds()
        self.stove.state["STATUS"] = 10              # someone pressed off on the stove
        self.tick(at(0, "06:01"))
        self.stove.state["STATUS"] = 0
        self.tick(at(0, "06:30"))
        self.assertEqual(self.cmds(), [])
        self.assertEqual(self.sched.state["phase"], "overridden")

    def test_reimported_running_slot_is_adopted_not_restarted(self):
        sid = slot(self.db, "evening", ["Mon"], "17:00", "22:00")
        self.tick(at(0, "17:00"))
        self.tick(at(0, "17:01"))
        self.cmds()
        self.stove.state["STATUS"] = 0               # switched off by hand at 18:30
        self.db.schedule_delete(sid)                 # schedule import --replace at 19:00
        new_id = slot(self.db, "evening", ["Mon"], "17:00", "22:00")
        self.tick(at(0, "19:00"))
        self.assertEqual(self.cmds(), [])
        self.assertEqual(self.sched.state["entry_id"], new_id)
        self.tick(at(0, "22:00"))                     # ...and it still ends normally
        self.assertEqual(self.sched.state["key"], None)

    def test_pending_off_is_tried_after_a_restart(self):
        slot(self.db, "s", ["Mon"], "06:00", "08:00")
        self.tick(at(0, "06:00"))
        self.tick(at(0, "06:01"))
        self.cmds()
        self.stove.fail = box_unreachable()          # router reboots at the end of the slot
        self.tick(at(0, "08:00"))
        self.stove.fail = None
        self.sched = self.new_scheduler()             # hub back 36 minutes later
        self.tick(at(0, "08:36"))
        self.assertEqual(self.cmds(), [("off", None)])

    def test_pending_off_from_before_a_long_outage_is_dropped(self):
        slot(self.db, "s", ["Mon"], "06:00", "08:00")
        self.tick(at(0, "06:00"))
        self.tick(at(0, "06:01"))
        self.cmds()
        self.stove.fail = box_unreachable()
        self.tick(at(0, "08:00"))
        self.stove.fail = None
        self.sched = self.new_scheduler()
        self.tick(at(0, "13:00"))
        self.assertEqual(self.cmds(), [])
        self.assertIsNone(self.sched.state["off"])

    def test_clock_going_back_does_not_run_a_finished_slot_again(self):
        slot(self.db, "night", ["Sat"], "22:00", "02:30")
        self.tick(at(5, "22:00"))
        self.tick(at(5, "22:01"))
        self.tick(at(6, "02:30"))                     # end: off
        self.assertEqual(self.cmds()[-1], ("off", None))
        self.stove.state["STATUS"] = 0
        self.tick(at(6, "02:05"))                     # 03:00 summer time -> 02:00 winter time
        self.tick(at(6, "02:20"))
        self.assertEqual(self.cmds(), [])

    def test_clock_going_back_after_another_slot_started(self):
        slot(self.db, "night", ["Sat"], "22:00", "02:30")
        slot(self.db, "early", ["Sun"], "02:40", "05:00", power=2)
        for t in ((5, "22:00"), (5, "22:01"), (6, "02:30"), (6, "02:40"), (6, "02:41")):
            self.tick(at(*t))
        self.cmds()
        self.tick(at(6, "02:05"))                     # clock back: 'night' must not come back
        self.assertNotEqual(self.sched.state.get("name"), "night")
        self.assertEqual([c for c in self.cmds() if c[0] == "on"], [])


if __name__ == "__main__":
    unittest.main()
