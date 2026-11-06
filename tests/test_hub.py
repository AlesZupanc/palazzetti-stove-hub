"""The hub's API (RPC) and the home-network server (no login, home network only)."""

import email.message
import json
import os
import socket
import struct
import tempfile
import threading
import time
import types
import unittest
import urllib.request

from helpers import ROOT, FakeStove, box_unreachable, make_config, temp_db

import stove_master
from stovemaster import lan, mdns, ws
from stovemaster.api import Client, Hub
from stovemaster.scheduler import Scheduler
from stovemaster.updates import Updates


class Recorder:
    """A Client whose messages are collected."""

    def __init__(self, via="lan", who="192.168.1.20"):
        self.got = []
        self.client = Client(via, who, self.got.append, ip=who)

    def events(self, name):
        return [m["d"] for m in self.got if m.get("ev") == name]


class HubBase(unittest.TestCase):
    def setUp(self):
        self.db = temp_db()
        self.cfg = make_config()
        self.stove = FakeStove()
        self.sched = Scheduler(self.db, self.stove, self.cfg)
        self.hub = Hub(self.cfg, self.db, self.stove, self.sched)
        self.rec = Recorder()
        self.hub.attach(self.rec.client)

    def call(self, method, params=None, client=None):
        reply = self.hub.handle(client or self.rec.client, {"id": 1, "m": method, "p": params or {}})
        if "e" in reply:
            return reply["code"], reply["e"]
        return 200, reply["r"]


class Rpc(HubBase):
    def test_hello_and_unknown(self):
        hello = self.rec.events("hello")[0]
        self.assertEqual((hello["via"], hello["who"], hello["device"]["name"]), ("lan", "192.168.1.20", "Stove"))
        self.assertEqual(self.call("nope")[0], 404)
        self.assertIsNone(self.hub.handle(self.rec.client, "junk"))
        self.assertEqual(self.hub.handle(self.rec.client, {"id": 3, "m": "status.get", "p": []})["code"], 400)

    def test_actions(self):
        self.assertEqual(self.call("stove.action", {"action": "power", "value": 9})[0], 400)
        status, body = self.call("stove.action", {"action": "power", "value": 5})
        self.assertEqual((status, body["power"]), (200, 5))
        self.call("stove.action", {"action": "on"})
        self.assertIn(("on", None), self.stove.commands)
        self.assertEqual(self.sched._manual[-1][0], "on")
        ev = self.db.events(1)[0]
        self.assertEqual((ev["actor"], ev["action"], ev["via"]), ("192.168.1.20", "on", "lan"))
        # everybody watching gets the new status and the activity entry at once
        self.assertEqual(self.rec.events("status")[-1]["phase"], "running")
        self.assertEqual(self.rec.events("activity")[-1]["action"], "on")

    def test_box_down(self):
        self.stove.fail = box_unreachable()
        status, body = self.call("status.get")
        self.assertEqual(status, 200)
        self.assertIn("timed out", body["error"])
        self.assertIn("scheduler", body)
        self.assertEqual(self.call("stove.action", {"action": "off"})[0], 502)
        self.assertEqual(self.db.events(1)[0]["ok"], 0)

    def test_pellet_counter_reset(self):
        self.assertEqual(self.call("status.get")[1]["pellets"], {"total": 10.0, "used": 10.0, "since": None})
        self.stove.state["PQT"] = 25
        status, body = self.call("pellets.reset")
        self.assertEqual(status, 200)
        self.assertEqual((body["pellets"]["used"], body["pellets"]["total"]), (0.0, 25.0))
        self.assertIsInstance(body["pellets"]["since"], int)
        self.assertEqual(self.rec.events("status")[-1]["pellets"]["used"], 0.0)     # every screen sees it
        ev = self.db.events(1)[0]
        self.assertEqual(ev["action"], "pellets reset")
        self.assertIn("25 kg counted", ev["detail"])
        self.assertEqual(self.stove.commands, [])                                    # the stove was not touched
        self.stove.state["PQT"] = 32
        self.assertEqual(self.call("status.get")[1]["pellets"]["used"], 7.0)
        self.stove.state["PQT"] = 3                                                  # the stove's own counter restarted
        self.assertEqual(self.call("status.get")[1]["pellets"]["used"], 3.0)
        del self.stove.state["PQT"]
        self.assertEqual(self.call("pellets.reset")[0], 409)
        self.assertNotIn("pellets", self.call("status.get")[1])
        self.stove.fail = box_unreachable()
        self.assertEqual(self.call("pellets.reset")[0], 502)

    def test_update_now(self):
        tmp = tempfile.mkdtemp()
        unit = os.path.join(tmp, "stove-master-update.path")
        self.hub.updates = Updates(tmp, unit)
        self.assertEqual(self.call("system.get")[1]["update"], {"available": False, "requested": None, "last": None})
        self.assertEqual(self.call("system.update")[0], 409)                    # automatic updates are off
        open(unit, "w").close()
        status, body = self.call("system.update")
        self.assertEqual((status, body["available"]), (200, True))
        self.assertIsInstance(body["requested"], int)
        self.assertTrue(os.path.exists(os.path.join(tmp, "update-request")))
        self.assertEqual(self.call("system.update")[0], 200)                    # twice is fine
        self.assertEqual(self.db.events(1)[0]["action"], "update")
        remote = Recorder("remote", "ana").client
        self.assertEqual(self.call("system.update", client=remote)[0], 403)     # only at home
        # what the updater wrote; a run that never finished counts as failed
        os.remove(os.path.join(tmp, "update-request"))
        with open(os.path.join(tmp, "update-status.json"), "w") as f:
            json.dump({"result": "running", "message": "Installing", "at": int(time.time()) - 3600}, f)
        last = self.call("system.get")[1]["update"]["last"]
        self.assertEqual(last["result"], "failed")
        with open(os.path.join(tmp, "update-request"), "w") as f:
            f.write("x")
        os.utime(os.path.join(tmp, "update-request"), (time.time() - 600, time.time() - 600))
        self.assertIn("did not start", self.call("system.get")[1]["update"]["stuck"])

    def test_probes(self):
        self.assertEqual(self.call("status.get")[1]["probes"], {"names": {}, "room": None})
        status, body = self.call("probes.set", {"names": {"T1": " Room ", "T3": "Flue gas", "T2": ""}, "room": "T1"})
        self.assertEqual((status, body), (200, {"names": {"T1": "Room", "T3": "Flue gas"}, "room": "T1"}))
        self.assertEqual(self.call("probes.set", {"names": {"T9": "x"}})[0], 400)
        self.assertEqual(self.call("probes.set", {"room": "T7"})[0], 400)

    def test_schedule(self):
        slot = {"name": "Morning", "days": [0, 1, 2, 3, 4], "start": "06:00", "end": "08:00",
                "power": 3, "fan": 7, "setpoint": 21.5}
        status, body = self.call("schedule.save", {"entry": slot})
        self.assertEqual(status, 200, body)
        sid = body["saved"]
        self.assertEqual(body["entries"][0]["fan_text"], "Auto")
        status, body = self.call("schedule.save", {"id": sid, "entry": dict(slot, end="02:00", days=["Sat"])})
        self.assertTrue(body["entries"][0]["overnight"])
        self.assertEqual(body["entries"][0]["days"], [5])
        self.assertEqual(self.call("schedule.save", {"entry": dict(slot, start="nope")})[0], 400)
        self.assertEqual(self.call("schedule.save", {"id": 999, "entry": slot})[0], 404)
        self.assertEqual(self.call("schedule.save", {"id": "1", "entry": slot})[0], 404)
        status, body = self.call("schedule.enable", {"enabled": False})
        self.assertEqual((status, body["enabled"]), (200, False))
        status, body = self.call("schedule.delete", {"id": sid})
        self.assertEqual((status, body["entries"]), (200, []))
        self.assertEqual([e["action"] for e in self.db.events(4)],
                         ["slot deleted", "schedule paused", "slot changed", "slot added"])
        self.assertEqual(len(self.rec.events("schedule")), 4)   # other open apps follow along

    def test_events_rename_system(self):
        self.call("stove.action", {"action": "fan", "value": 6})
        status, body = self.call("events.list", {"limit": 1})
        self.assertEqual((body["events"][0]["detail"], body["events"][0]["ok"]), ("fan High", True))
        self.assertEqual(self.call("device.rename", {"name": ""})[0], 400)
        self.assertEqual(self.call("device.rename", {"name": "Living room"})[1], {"name": "Living room"})
        status, body = self.call("system.get")
        self.assertEqual((body["device"]["name"], body["cloud"]), ("Living room", {"configured": False}))

    def test_home_only_methods(self):
        remote = Recorder("remote", "ana").client
        self.assertEqual(self.call("cloud.info", client=remote)[0], 403)
        self.assertEqual(self.call("cloud.info")[1], {"configured": False})
        pairing = Client("remote", "eve", lambda m: None, pairing=True, profile={"id": "9", "name": "eve"})
        self.assertEqual(self.call("status.get", client=pairing)[0], 403)


class PelletTank(HubBase):
    def tank(self):
        return self.call("status.get")[1]["pellets"].get("tank")

    def test_no_tank_by_default(self):
        body = self.call("status.get")[1]
        self.assertNotIn("tank", body["pellets"])
        self.assertEqual(body["pellet_tank"], {"size": None, "low_pct": 20})

    def test_size_and_what_is_left(self):
        self.call("pellets.refill")                                   # filled up at PQT 10
        status, body = self.call("pellets.tank", {"size": 15, "low_pct": 20})
        self.assertEqual(status, 200)
        self.assertEqual(body["pellet_tank"], {"size": 15.0, "low_pct": 20})
        self.assertEqual(self.rec.events("status")[-1]["pellets"]["tank"]["left"], 15.0)   # every screen sees it
        self.stove.state["PQT"] = 16                                   # 6 kg burnt
        t = self.tank()
        self.assertEqual((t["left"], t["pct"], t["low"], t["empty"], t["over"]), (9.0, 60, False, False, False))
        self.stove.state["PQT"] = 23                                   # 13 kg burnt: 2 kg left, below 20 %
        t = self.tank()
        self.assertEqual((t["left"], t["pct"], t["low"]), (2.0, 13, True))
        self.stove.state["PQT"] = 30                                   # more than the tank holds
        t = self.tank()
        self.assertEqual((t["left"], t["pct"], t["over"]), (0.0, 0, True))
        ev = [e for e in self.db.events(5) if e["action"] == "pellet tank"][0]
        self.assertIn("15 kg, a warning below 20 %", ev["detail"])
        self.assertEqual(self.call("pellets.tank", {"size": None})[1]["pellet_tank"], {"size": None, "low_pct": 20})
        self.assertIsNone(self.tank())
        self.assertEqual(self.stove.commands, [])                      # the stove is never touched

    def test_refill_full_and_partial(self):
        self.call("pellets.tank", {"size": 20})
        self.assertEqual(self.call("pellets.refill", {"added": 5})[0], 200)   # (left 20 - 10 used + 5 > 20: full)
        self.call("pellets.refill")
        self.stove.state["PQT"] = 25                                   # 15 kg burnt: 5 left
        self.assertEqual(self.tank()["left"], 5.0)
        status, body = self.call("pellets.refill", {"added": 7.5})
        self.assertEqual((status, body["pellets"]["tank"]["left"], body["pellets"]["used"]), (200, 12.5, 0.0))
        ev = self.db.events(1)[0]
        self.assertEqual(ev["action"], "pellets refilled")
        self.assertIn("7.5 kg added, about 12.5 of 20 kg in the tank; 15 kg burnt since the last refill", ev["detail"])
        self.stove.state["PQT"] = 27
        P = self.call("status.get")[1]["pellets"]
        self.assertEqual((P["tank"]["left"], P["used"]), (10.5, 2.0))  # used: really burnt since that refill
        self.call("pellets.tank", {"size": 30})                        # a bigger size keeps the level
        self.assertEqual(self.tank()["left"], 10.5)
        self.call("pellets.tank", {"size": 8})                         # ... a smaller one caps it
        self.assertEqual(self.tank()["left"], 8.0)
        self.call("pellets.tank", {"size": 20})
        status, body = self.call("pellets.refill")
        self.assertEqual((body["pellets"]["tank"]["left"], body["pellets"]["used"]), (20.0, 0.0))
        self.assertIn("tank filled up; 2 kg burnt", self.db.events(1)[0]["detail"])

    def test_bad_input(self):
        for p in ({"size": 0.5}, {"size": 500}, {"size": "big"}, {"size": True}, {"size": 15, "low_pct": 95},
                  {"size": 15, "low_pct": "x"}):
            self.assertEqual(self.call("pellets.tank", p)[0], 400, p)
        self.assertEqual(self.call("pellets.refill", {"added": 3})[0], 409)   # no tank size yet
        self.call("pellets.tank", {"size": 15})
        for added in (0, -2, 900, "lots", True):
            self.assertEqual(self.call("pellets.refill", {"added": added})[0], 400, added)
        del self.stove.state["PQT"]
        self.assertEqual(self.call("pellets.refill")[0], 409)
        self.stove.fail = box_unreachable()
        self.assertEqual(self.call("pellets.refill")[0], 502)
        status, body = self.call("pellets.tank", {"size": 18})        # saved even while the box is away
        self.assertEqual((status, body["pellet_tank"]["size"]), (200, 18.0))

    def test_burn_rate_and_hours_left(self):
        self.call("pellets.tank", {"size": 15})
        self.call("pellets.refill")
        self.assertIsNone(self.hub.pellet_rate())
        # 2 h of burning at 1.5 kg/h, sampled every minute, with an hour switched off in between
        t, kg = 1000.0, 10.0
        for minute in range(180):
            burning = not 60 <= minute < 120
            self.hub.pellet_sample(round(kg, 2), burning, now=t)
            t += 60
            if burning:
                kg += 1.5 / 60
        self.assertAlmostEqual(self.hub.pellet_rate(), 1.5, places=1)
        self.stove.state["PQT"] = 13                                   # 3 kg burnt: 12 left = 8 h
        self.assertAlmostEqual(self.tank()["hours_left"], 8.0, delta=0.3)
        self.hub.pellet_sample(1.0, True, now=t)                       # the stove's counter started again
        self.assertIsNone(self.hub.pellet_rate())

    def test_siri_mentions_the_tank(self):
        from stovemaster import urlapi
        self.call("pellets.tank", {"size": 15})
        self.call("pellets.refill")
        self.stove.state["PQT"] = 16
        state = self.call("status.get")[1]
        self.assertIn("Pellets: about 9 kilos left, 60 percent of the tank.", urlapi.describe_state(state, {}))

    def test_burn_rate_from_a_counter_of_whole_kg(self):
        # 1.2 kg/h, but the counter only shows whole kg (and stood at 41.95 when the fire started)
        t, kg = 1000.0, 41.95
        rates = []
        for minute in range(4 * 60):
            self.hub.pellet_sample(float(int(kg)), True, now=t)
            rates.append(self.hub.pellet_rate())
            t += 60
            kg += 1.2 / 60
        self.assertIsNone(rates[60])                                   # an hour, but only from step to step
        self.assertTrue(all(r is None or abs(r - 1.2) < 0.15 for r in rates), rates)
        self.assertAlmostEqual(rates[-1], 1.2, delta=0.05)

    def test_level_in_percent(self):
        self.call("pellets.tank", {"size": 20, "low_pct": 20})
        self.call("pellets.refill")                                    # filled up at PQT 10
        self.stove.state["PQT"] = 18                                   # 8 kg burnt: 12 left (60 %)
        status, body = self.call("pellets.level", {"pct": 50})        # looked in: only half
        P = body["pellets"]
        self.assertEqual((status, P["tank"]["left"], P["tank"]["pct"], P["used"]), (200, 10.0, 50, 8.0))
        ev = self.db.events(1)[0]                                      # a correction: still the same filling
        self.assertEqual((ev["action"], ev["detail"]), ("pellet level",
                         "about 50 % in the tank (10 of 20 kg); the estimate was 60 %"))
        self.stove.state["PQT"] = 21                                   # counts down from there
        self.assertEqual(self.tank()["left"], 7.0)
        status, body = self.call("pellets.level", {"pct": 90})        # clearly more: pellets were put in
        P = body["pellets"]
        self.assertEqual((P["tank"]["left"], P["used"]), (18.0, 0.0))
        self.assertEqual(self.db.events(1)[0]["action"], "pellets refilled")
        self.assertIn("the tank is about 90 % full now (18 of 20 kg); 11 kg burnt", self.db.events(1)[0]["detail"])
        self.assertEqual(self.call("pellets.level", {"pct": 100})[1]["pellets"]["tank"]["left"], 20.0)   # +2 kg: a correction
        self.assertEqual(self.db.events(1)[0]["action"], "pellet level")
        self.assertEqual(self.stove.commands, [])

    def test_level_starts_a_filling_when_the_estimate_is_wrong(self):
        self.call("pellets.tank", {"size": 15})                        # never reset: the lifetime counter
        status, body = self.call("pellets.level", {"pct": 10})
        P = body["pellets"]
        self.assertEqual((P["tank"]["left"], P["used"], P["tank"]["over"]), (1.5, 0.0, False))
        self.assertEqual(self.db.events(1)[0]["action"], "pellets refilled")
        self.stove.state["PQT"] = 40                                   # more than the tank holds: "over"
        self.assertTrue(self.tank()["over"])
        self.assertEqual(self.call("pellets.level", {"pct": 20})[1]["pellets"]["used"], 0.0)   # a new filling

    def test_level_follows_a_corrected_size_and_shows_the_pct_set(self):
        self.call("pellets.tank", {"size": 20})
        self.call("pellets.refill")
        self.call("pellets.level", {"pct": 50})                       # 10 of 20 kg
        self.call("pellets.tank", {"size": 15})                        # the manual says 15 kg after all
        self.assertEqual((self.tank()["pct"], self.tank()["left"]), (50, 7.5))
        self.call("pellets.tank", {"size": 8.5})
        self.call("pellets.level", {"pct": 50})
        self.assertEqual(self.tank()["pct"], 50)                       # (4.25 kg, not 4.2 = 49 %)

    def test_a_low_level_and_a_counter_of_whole_kg(self):
        self.call("pellets.tank", {"size": 15})
        self.call("pellets.refill")
        self.stove.state["PQT"] = 20                                   # 5 kg left
        self.call("pellets.level", {"pct": 5})                         # looked in: 0.75 kg
        self.stove.state["PQT"] = 21                                   # the counter steps a whole kg
        t = self.tank()
        self.assertEqual((t["left"], t["over"]), (0.0, False))         # not "more than the tank holds"
        self.call("pellets.level", {"pct": 5})
        self.assertEqual(self.db.events(1)[0]["action"], "pellet level")   # ... so not a refill either
        self.assertEqual(self.call("status.get")[1]["pellets"]["used"], 11.0)

    def test_saving_the_tank_unchanged_keeps_the_warning_off(self):
        self.call("pellets.tank", {"size": 15, "low_pct": 20})
        self.call("pellets.refill")
        self.call("pellets.level", {"pct": 10})                        # they know it is low
        warned = self.db.get_setting("pellets_low_warned")
        self.call("pellets.tank", {"size": 15, "low_pct": 20})
        self.assertEqual(self.db.get_setting("pellets_low_warned"), warned)
        self.call("pellets.tank", {"size": 15, "low_pct": 30})         # another level may warn again
        self.assertEqual(self.db.get_setting("pellets_low_warned"), {})

    def test_level_bad_input(self):
        for pct in (None, -5, 101, "50", True, [50]):
            self.assertEqual(self.call("pellets.level", {"pct": pct})[0], 400, pct)
        self.assertEqual(self.call("pellets.level", {"pct": 50})[0], 409)   # no tank size
        self.call("pellets.tank", {"size": 15})
        self.stove.fail = box_unreachable()
        self.assertEqual(self.call("pellets.level", {"pct": 50})[0], 502)
        self.stove.fail = None
        del self.stove.state["PQT"]
        self.assertEqual(self.call("pellets.level", {"pct": 50})[0], 409)

    def test_more_than_the_tank_holds_cannot_be_added(self):
        self.call("pellets.tank", {"size": 15})
        self.call("pellets.refill")
        self.stove.state["PQT"] = 20                                   # 10 kg burnt: 5 left
        self.assertEqual(self.call("pellets.refill", {"added": 75}), (400, "the tank holds only 15 kg"))
        status, body = self.call("pellets.refill", {"added": 12})
        self.assertEqual((status, body["pellets"]["tank"]["left"]), (200, 15.0))
        self.assertIn("12 kg added, the tank is full; 10 kg burnt", self.db.events(1)[0]["detail"])


class Telemetry(HubBase):
    def run_telemetry(self, steps):
        from stovemaster import api
        old = api.FAST_POLL
        api.FAST_POLL = 0.05
        try:
            self.hub.start()
            time.sleep(0.3)
            for step in steps:
                step()
                time.sleep(0.4)
        finally:
            self.hub.stop()
            api.FAST_POLL = old

    def test_empty_tank_from_the_no_pellets_alarm(self):
        notes = []
        self.hub.notify = lambda cat, title, body, tag=None: notes.append((cat, title, tag))
        self.call("pellets.tank", {"size": 15})
        self.call("pellets.refill")
        self.stove.state.update(PQT=21, STATUS=6)                      # 11 kg burnt
        self.run_telemetry([lambda: self.stove.state.update(STATUS=253)])
        self.assertEqual(notes, [("problems", "Pellet tank empty", "pellets")])   # not the generic alarm one
        t = self.rec.events("status")[-1]["pellets"]["tank"]
        self.assertEqual((t["empty"], t["left"], t["pct"]), (True, 0.0, 0))
        actions = [e["action"] for e in self.db.events(5)]
        self.assertIn("alarm", actions)
        ev = [e for e in self.db.events(5) if e["action"] == "pellets empty"][0]
        self.assertIn("11 kg were burnt", ev["detail"])                # the tank's real content: for the size
        status, body = self.call("pellets.refill", {"added": 10})     # an empty tank + 10 kg
        self.assertEqual((body["pellets"]["tank"]["empty"], body["pellets"]["tank"]["left"]), (False, 10.0))

    def test_running_low_is_reported_once_per_fill(self):
        notes = []
        self.hub.notify = lambda cat, title, body, tag=None: notes.append((title, body))
        self.call("pellets.tank", {"size": 15, "low_pct": 20})
        self.call("pellets.refill")
        self.stove.state.update(PQT=20, STATUS=6)                      # 10 kg burnt: 5 left, fine
        seen = []
        self.run_telemetry([lambda: self.stove.state.update(PQT=23),  # 2 left: low
                            lambda: self.stove.state.update(PQT=24),  # still low: no second message
                            lambda: seen.append(list(notes)),
                            lambda: self.call("pellets.refill"),      # a new fill may warn again
                            lambda: self.stove.state.update(PQT=37)])
        self.assertEqual([n[0] for n in seen[0]], ["Pellets running low"])
        self.assertIn("About 2 kg left in the tank (13 %)", seen[0][0][1])
        self.assertEqual([n[0] for n in notes], ["Pellets running low", "Pellets running low"])
        self.assertEqual([e["action"] for e in self.db.events(10)].count("pellets low"), 2)

    def notes(self):
        notes = []
        self.hub.notify = lambda cat, title, body, tag=None: notes.append((title, tag))
        return notes

    def test_no_pellets_alarm_without_a_pellet_counter(self):
        notes = self.notes()
        del self.stove.state["PQT"]
        self.stove.state["STATUS"] = 6
        self.run_telemetry([lambda: self.stove.state.update(STATUS=253)])
        self.assertEqual(notes, [("Stove alarm", "alarm")])             # the generic message, not nothing

    def test_no_pellets_alarm_when_the_bookkeeping_fails(self):
        notes = self.notes()
        self.call("pellets.tank", {"size": 15})
        self.stove.state["STATUS"] = 6

        def broken(*a):
            raise RuntimeError("disk full")
        self.run_telemetry([lambda: setattr(self.hub, "_set_fill", broken),
                            lambda: setattr(self.hub.db, "set_setting", broken),
                            lambda: self.stove.state.update(STATUS=253)])
        self.assertEqual(notes, [("Stove alarm", "alarm")])

    def test_refilled_while_the_fire_died_down(self):
        notes = self.notes()
        self.call("pellets.tank", {"size": 15})
        self.call("pellets.refill")
        self.stove.state.update(PQT=24, STATUS=6)                      # 14 kg burnt, the flame dies down
        self.run_telemetry([lambda: self.call("pellets.refill"),       # filled and tapped before the alarm
                            lambda: self.stove.state.update(STATUS=253)])
        self.assertEqual(notes, [("Pellets running low", "pellets"), ("Stove alarm: no pellets", "pellets")])
        t = self.rec.events("status")[-1]["pellets"]["tank"]
        self.assertEqual((t["empty"], t["left"]), (False, 15.0))         # the new filling is not empty

    def test_filled_without_tapping_tank_refilled(self):
        self.notes()
        self.call("pellets.tank", {"size": 15})
        self.call("pellets.refill")
        self.stove.state.update(PQT=24, STATUS=6)
        self.run_telemetry([lambda: self.stove.state.update(STATUS=253)])
        self.assertTrue(self.call("status.get")[1]["pellets"]["tank"]["empty"])
        self.stove.state.update(STATUS=6, PQT=26)                      # filled, alarm reset on the panel: burns
        t = self.call("status.get")[1]["pellets"]["tank"]
        self.assertEqual((t["empty"], t["over"], t["left"]), (False, True, 0.0))   # "Filled up without tapping?"

    def test_first_tank_size_on_an_old_counter_does_not_warn(self):
        notes = self.notes()
        self.call("pellets.reset")                                     # the old Reset, long ago: 10 kg
        self.stove.state.update(PQT=30, STATUS=6)                      # 20 kg burnt since
        self.run_telemetry([lambda: self.call("pellets.tank", {"size": 15}),
                            lambda: self.stove.state.update(PQT=31)])
        self.assertEqual(notes, [])                                    # "over": what is left is not known
        self.assertTrue(self.call("status.get")[1]["pellets"]["tank"]["over"])

    def test_low_warning_without_any_refill_mark(self):
        notes = self.notes()
        self.stove.state.update(PQT=4, STATUS=6)                       # a new stove, never reset
        self.call("pellets.tank", {"size": 15})
        self.run_telemetry([lambda: self.stove.state.update(PQT=13)])  # 2 kg left
        self.assertEqual(notes, [("Pellets running low", "pellets")])

    def test_level_and_the_running_low_message(self):
        notes = self.notes()
        self.call("pellets.tank", {"size": 20, "low_pct": 20})
        self.call("pellets.refill")
        self.stove.state.update(PQT=12, STATUS=6)                      # 18 kg left
        self.run_telemetry([lambda: self.call("pellets.level", {"pct": 10}),   # they know: no message
                            lambda: self.call("pellets.level", {"pct": 50}),   # wrong after all: may warn again
                            lambda: self.stove.state.update(PQT=19)])          # 3 kg left
        self.assertEqual(notes, [("Pellets running low", "pellets")])

    def test_level_after_the_tank_ran_empty(self):
        self.notes()
        self.call("pellets.tank", {"size": 15})
        self.call("pellets.refill")
        self.stove.state.update(PQT=24, STATUS=6)
        self.run_telemetry([lambda: self.stove.state.update(STATUS=253)])
        self.assertTrue(self.tank()["empty"])
        t = self.call("pellets.level", {"pct": 5})[1]["pellets"]["tank"]   # a little put in: a new filling
        self.assertEqual((t["empty"], t["left"]), (False, 0.8))

    def test_zero_percent_confirms_an_empty_tank(self):
        notes = self.notes()
        self.call("pellets.tank", {"size": 15})
        self.call("pellets.refill")
        self.stove.state.update(PQT=24, STATUS=6)
        self.run_telemetry([lambda: self.stove.state.update(STATUS=253),
                            lambda: self.call("pellets.level", {"pct": 0}),     # yes, it is empty
                            lambda: self.stove.state.update(STATUS=6),          # alarm reset, nothing put in
                            lambda: self.stove.state.update(STATUS=253)])
        t = self.tank()
        self.assertEqual((t["empty"], self.call("status.get")[1]["pellets"]["used"]), (True, 14.0))
        self.assertIn("pellet level", [e["action"] for e in self.db.events(10)])
        self.assertEqual([n[0] for n in notes], ["Pellets running low", "Pellet tank empty", "Pellet tank empty"])

    def test_a_filling_started_by_a_level_runs_out(self):
        notes = self.notes()
        self.stove.state.update(PQT=123, STATUS=6)                     # never reset
        self.call("pellets.tank", {"size": 15})
        self.call("pellets.level", {"pct": 5})                         # first size: "about 5 %" (a filling)
        self.run_telemetry([lambda: self.stove.state.update(PQT=123.4),
                            lambda: self.stove.state.update(STATUS=253)])
        self.assertEqual(notes[-1], ("Pellet tank empty", "pellets"))   # not "just after the tank was refilled"
        self.assertTrue(self.tank()["empty"])

    def test_zero_percent_on_an_empty_tank_never_refilled(self):
        self.notes()
        self.stove.state.update(PQT=123, STATUS=6)                     # never reset
        self.call("pellets.tank", {"size": 15})
        self.run_telemetry([lambda: self.stove.state.update(STATUS=253)])
        self.call("pellets.level", {"pct": 0})
        self.assertTrue(self.tank()["empty"])
        self.assertEqual(self.db.events(1)[0]["action"], "pellet level")

    def test_hub_restarts_during_the_alarm_after_a_level(self):
        notes = self.notes()
        self.call("pellets.tank", {"size": 15})
        self.call("pellets.refill")
        self.stove.state.update(PQT=24, STATUS=6)
        self.run_telemetry([lambda: self.stove.state.update(STATUS=253),
                            lambda: self.call("pellets.level", {"pct": 5})])    # a little put in
        self.assertEqual(self.tank()["left"], 0.8)
        def restart():
            self.hub = Hub(self.cfg, self.db, self.stove, self.sched)
            self.hub.notify = lambda cat, title, body, tag=None: notes.append((title, tag))
            self.hub.attach(self.rec.client)                                    # (watched: it polls fast)
        restart()                                                               # the alarm is still on
        self.run_telemetry([])
        t = self.tank()
        self.assertEqual((t["empty"], t["left"]), (False, 0.8))                 # the level stays
        self.assertEqual(sum(e["action"] == "pellets empty" for e in self.db.events(20)), 1)
        restart()
        self.run_telemetry([lambda: self.stove.state.update(STATUS=6),          # alarm reset: burns it ...
                            lambda: self.stove.state.update(PQT=24.6, STATUS=253)])   # ... and runs out again
        self.assertTrue(self.tank()["empty"])
        self.assertEqual(notes[-1], ("Pellet tank empty", "pellets"))

    def tank(self):
        return self.call("status.get")[1]["pellets"].get("tank")

    def test_hub_started_during_the_no_pellets_alarm(self):
        notes = self.notes()
        self.call("pellets.tank", {"size": 15})
        self.call("pellets.refill")
        self.stove.state.update(PQT=21, STATUS=253)                    # ran out while the hub was down
        self.run_telemetry([])
        self.assertEqual(notes, [])                                    # no message for an old alarm ...
        t = self.rec.events("status")[-1]["pellets"]["tank"]
        self.assertEqual((t["empty"], t["left"]), (True, 0.0))           # ... but the tank is empty
        self.assertIn("11 kg were burnt", [e for e in self.db.events(5) if e["action"] == "pellets empty"][0]["detail"])


    def test_alarm_is_reported_once(self):
        notes = []
        self.hub.notify = lambda cat, title, body, tag=None: notes.append((cat, title, tag))
        from stovemaster import api
        old = api.FAST_POLL
        api.FAST_POLL = 0.05
        try:
            self.hub.start()
            time.sleep(0.3)
            self.stove.state["STATUS"] = 241           # an alarm code
            time.sleep(0.4)
        finally:
            self.hub.stop()
            api.FAST_POLL = old
        self.assertEqual(notes, [("problems", "Stove alarm", "alarm")])
        self.assertEqual(self.rec.events("status")[-1]["phase"], "alarm")
        self.assertEqual(self.db.events(1)[0]["action"], "alarm")


class LanChecks(unittest.TestCase):
    def test_addresses(self):
        for ip in ("192.168.1.5", "10.0.0.2", "172.20.1.1", "127.0.0.1", "::1", "fe80::1", "fd00::5",
                   "100.101.102.103", "::ffff:192.168.1.9", "169.254.3.4"):
            self.assertTrue(lan.ip_allowed(ip), ip)
        for ip in ("8.8.8.8", "2a00:1450::1", "::ffff:8.8.8.8", "nonsense"):
            self.assertFalse(lan.ip_allowed(ip), ip)

    def test_split_host(self):
        self.assertEqual(lan.split_host("Stove.local:8080"), "stove.local")
        self.assertEqual(lan.split_host("[fe80::1]:80"), "fe80::1")
        self.assertEqual(lan.split_host("fe80::1"), "fe80::1")
        self.assertEqual(lan.split_host("stove.local."), "stove.local")

    @staticmethod
    def request(peer, trust_proxy, **headers):
        msg = email.message.Message()
        for k, v in headers.items():
            msg[k.replace("_", "-")] = v
        return types.SimpleNamespace(client_address=(peer, 40000), headers=msg,
                                     server=types.SimpleNamespace(cfg=types.SimpleNamespace(trust_proxy=trust_proxy)))

    def test_client_ip_behind_a_proxy(self):
        def ip(peer, trust, real=None):
            r = self.request(peer, trust) if real is None else self.request(peer, trust, X_Real_IP=real)
            return lan.Handler.client_ip(r)
        # a reverse proxy on this Pi: the connection comes from 127.0.0.1, the visitor is in X-Real-IP
        self.assertEqual(ip("127.0.0.1", True, "192.168.1.40"), "192.168.1.40")
        self.assertEqual(ip("::ffff:127.0.0.1", True, " fe80::7 "), "fe80::7")
        self.assertEqual(ip("127.0.0.1", True, "8.8.8.8"), "8.8.8.8")          # refused later by lan_only
        for junk in ("nonsense", "", "192.168.1.40, 8.8.8.8", "1.2.3.4.5"):
            self.assertEqual(ip("127.0.0.1", True, junk), "", junk)            # tampered: refused
        self.assertEqual(ip("127.0.0.1", True), "127.0.0.1")                   # a program on the Pi itself
        # the header counts only with trust_proxy and only from 127.0.0.1
        self.assertEqual(ip("127.0.0.1", False, "192.168.1.40"), "127.0.0.1")
        self.assertEqual(ip("192.168.1.9", True, "10.0.0.1"), "192.168.1.9")
        self.assertEqual(ip("8.8.8.8", True, "192.168.1.40"), "8.8.8.8")

    def test_local_program(self):
        self.assertTrue(lan.Handler.local_program(self.request("127.0.0.1", True)))
        self.assertTrue(lan.Handler.local_program(self.request("::1", False)))
        self.assertFalse(lan.Handler.local_program(self.request("127.0.0.1", True, X_Real_IP="127.0.0.1")))
        self.assertFalse(lan.Handler.local_program(self.request("127.0.0.1", False, X_Real_IP="")))
        self.assertFalse(lan.Handler.local_program(self.request("192.168.1.9", False)))

    def test_public_port(self):
        cases = (({}, 80, "http://stove.local/"),
                 ({"web_port": "8080"}, 8080, "http://stove.local:8080/"),
                 ({"web_bind": "127.0.0.1", "web_port": "8080", "web_public_port": "80"}, 80, "http://stove.local/"),
                 ({"web_port": "80", "web_public_port": "8443"}, 8443, "http://stove.local:8443/"))
        for values, port, url in cases:
            cfg = make_config(**values)
            self.assertEqual(cfg.public_port, port, values)
            db = temp_db()
            self.assertEqual(Hub(cfg, db, FakeStove(), Scheduler(db, FakeStove(), cfg)).lan_url(), url, values)
            # the DNS-SD record of stove.local names the port browsers use, not the hub's own
            target = mdns.encode_name("stove.local")
            srv = stove_master.mdns_responder(cfg)._srv(120)
            self.assertEqual(srv[-len(target):], target)
            self.assertEqual(struct.unpack("!HHH", srv[-len(target) - 6:-len(target)]), (0, 0, port), values)


class LanServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = temp_db()
        cfg = make_config(web_bind="127.0.0.1", web_port="0", mdns_name="kamin", web_allowed_hosts="pi.lan")
        cls.stove = FakeStove()
        cls.hub = Hub(cfg, cls.db, cls.stove, Scheduler(cls.db, cls.stove, cfg))
        cls.server = lan.LanServer(cls.hub, cfg)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def get(self, path, host=None, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers or {})
        if host:
            req.add_header("Host", host)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=5) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def test_app_files_and_env(self):
        status, body, h = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"app.js", body)
        self.assertIn(f"ws://127.0.0.1:{self.port}", h["Content-Security-Policy"])
        status, body, h = self.get("/theme.js")
        self.assertTrue(h["Content-Type"].startswith("text/javascript"))
        env = json.loads(self.get("/env.json")[1])
        self.assertEqual((env["mode"], env["device"]["name"], env["cloud_url"]), ("local", "Stove", None))
        self.assertEqual(self.get("/nope")[0], 404)
        self.assertEqual(self.get("/../stovemaster/api.py")[0], 404)

    def test_host_names(self):
        for host in ("kamin.local", "KAMIN.local:80", "pi.lan", "localhost:1", "192.168.1.9", "[fe80::2]:80",
                     socket.gethostname()):
            self.assertEqual(self.get("/env.json", host)[0], 200, host)
        for host in ("evil.example.com", "kamin.local.evil.com", ""):
            self.assertEqual(self.get("/env.json", host or " ")[0], 421, host)

    def test_websocket_rpc_and_origin(self):
        url = f"ws://127.0.0.1:{self.port}/ws"
        with self.assertRaises(ws.HandshakeError) as ctx:
            ws.connect(url, headers=[("Origin", "https://evil.example.com")])
        self.assertEqual(ctx.exception.status, 403)
        c = ws.connect(url, headers=[("Origin", f"http://127.0.0.1:{self.port}")])
        try:
            first = json.loads(c.recv())
            self.assertEqual((first["ev"], first["d"]["via"]), ("hello", "lan"))
            c.send(json.dumps({"id": 1, "m": "stove.action", "p": {"action": "setpoint", "value": 22.5}}))
            reply = None
            while reply is None:
                msg = json.loads(c.recv())
                if msg.get("id") == 1:
                    reply = msg
            self.assertEqual(reply["r"]["setpoint"], 22.5)
            self.assertEqual(self.db.events(1)[0]["actor"], "127.0.0.1")
        finally:
            c.close()

    def test_outside_addresses_are_refused(self):
        self.server.cfg.lan_only = True
        old = lan.ip_allowed
        lan.ip_allowed = lambda ip: False
        try:
            status, body, _ = self.get("/")
            self.assertEqual(status, 403)
            self.assertIn(b"home network", body)
        finally:
            lan.ip_allowed = old

    def test_x_real_ip_is_ignored_without_trust_proxy(self):
        # no proxy in front: anybody can send the header, so it means nothing
        self.assertEqual(self.get("/env.json", headers={"X-Real-IP": "8.8.8.8"})[0], 200)
        self.assertEqual(self.get("/env.json", headers={"X-Real-IP": "nonsense"})[0], 200)
        status, body, _ = self.get("/env.json", "evil.example.com")
        self.assertEqual(status, 421)
        self.assertIn(b"http://kamin.local:", body)
        self.assertIn(b"or the hub's IP address", body)


class BehindAProxy(unittest.TestCase):
    """The hub on 127.0.0.1 behind a reverse proxy on the Pi, e.g. nginx on port 80 ([web] trust_proxy)."""

    @classmethod
    def setUpClass(cls):
        cls.db = temp_db()
        cfg = make_config(web_bind="127.0.0.1", web_port="0", web_public_port="80", web_trust_proxy="yes",
                          mdns_name="kamin")
        cls.stove = FakeStove()
        cls.hub = Hub(cfg, cls.db, cls.stove, Scheduler(cls.db, cls.stove, cfg))
        cls.server = lan.LanServer(cls.hub, cfg)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    get = LanServer.get

    def test_the_visitor_address_comes_from_the_proxy(self):
        for real in ("192.168.1.40", "10.1.2.3", "fd00::9", "100.100.1.1"):
            self.assertEqual(self.get("/env.json", "kamin.local", {"X-Real-IP": real})[0], 200, real)
        for real in ("8.8.8.8", "2a00:1450::1", "::ffff:8.8.8.8", "nonsense", "", "1.1.1.1, 192.168.1.4"):
            status, body, _ = self.get("/env.json", "kamin.local", {"X-Real-IP": real})
            self.assertEqual((status, b"home network" in body), (403, True), real)
        self.assertEqual(self.get("/env.json")[0], 200)              # straight from the Pi itself

    def test_garbage_is_refused_even_without_lan_only(self):
        self.server.cfg.lan_only = False
        try:
            self.assertEqual(self.get("/env.json", "kamin.local", {"X-Real-IP": "8.8.8.8"})[0], 200)
            self.assertEqual(self.get("/env.json", "kamin.local", {"X-Real-IP": "nonsense"})[0], 403)
        finally:
            self.server.cfg.lan_only = True

    def test_activity_names_the_visitor(self):
        url = f"ws://127.0.0.1:{self.port}/ws"
        page = f"http://127.0.0.1:{self.port}"
        c = ws.connect(url, headers=[("Origin", page), ("X-Real-IP", "192.168.1.77")])
        try:
            hello = json.loads(c.recv())
            self.assertEqual((hello["d"]["who"], hello["d"]["lan_url"]), ("192.168.1.77", "http://kamin.local/"))
            c.send(json.dumps({"id": 1, "m": "stove.action", "p": {"action": "fan", "value": 3}}))
            while json.loads(c.recv()).get("id") != 1:
                pass
            self.assertEqual(self.db.events(1)[0]["actor"], "192.168.1.77")
        finally:
            c.close()
        with self.assertRaises(ws.HandshakeError) as ctx:
            ws.connect(url, headers=[("Origin", page), ("X-Real-IP", "8.8.4.4")])
        self.assertEqual(ctx.exception.status, 403)

    def test_unknown_names_point_to_stove_local_not_the_ip(self):
        status, body, _ = self.get("/env.json", "evil.example.com", {"X-Real-IP": "192.168.1.40"})
        self.assertEqual(status, 421)
        self.assertIn(b"Open http://kamin.local/ instead.", body)
        self.assertNotIn(b"the hub's IP address", body)       # where it goes is up to the proxy


class AppFiles(unittest.TestCase):
    def test_app_dir_and_file_map(self):
        d = lan.find_app_dir()
        self.assertEqual(os.path.realpath(d), os.path.realpath(os.path.join(ROOT, "www", "app")))
        files = lan.load_app_files(d)
        self.assertIs(files["/"], files["/index.html"])
        self.assertIn("/app.js", files)


if __name__ == "__main__":
    unittest.main()
