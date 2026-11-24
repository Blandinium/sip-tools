import asyncio
import configparser
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import time
import unittest

from mock_services import MockAMI, MockPhone, MockTG, wire
from sms_bridge import Bridge, INCOMPLETE, Queue, Settings, normalize


SCRIPT = Path(__file__).resolve().parents[1] / "sms_bridge.py"
EXTERNAL = "+12025550199"
PRIMARY = "+12025550101"
SECONDARY = "+12025550102"
ALIAS = "+447700900102"


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.ami, self.tg, self.other_tg = MockAMI(), MockTG(), MockTG()
        for mock in (self.ami, self.tg, self.other_tg):
            await mock.start()
            self.addAsyncCleanup(mock.stop)
        self.phone1, self.phone2 = await MockPhone().start(), await MockPhone().start()
        for phone in (self.phone1, self.phone2):
            self.addCleanup(phone.transport.close)
        self.ami.contacts("2001", self.phone1)
        self.ami.contacts("2002", self.phone2)
        self.ami.contacts("other-aor", self.phone2)
        credentials = self.directory / "credentials"
        credentials.write_text("test-account:test-placeholder\n")
        alerts = self.directory / "alerts"
        self.alerts = alerts
        hook = self.directory / "notify"
        hook.write_text(f'#!/bin/sh\nprintf "%s\\n" "$1" >> "{alerts}"\n')
        hook.chmod(0o700)
        self.config = self.directory / "bridge.conf"
        self.config.write_text(f"""[bridge]
ami_credentials_file = {credentials}
ami_port = {self.ami.port}
sip_domain = pbx.example.net
sip_bind = 127.0.0.1
db_path = {self.directory / "queue.sqlite3"}
notify_hook = {hook}
repeat_hours = 0.0001
retry_hours = 0.00015
retry_seconds = 0.05
multipart_seconds = 0.1
api_timeout_seconds = 0.2
send_status_seconds = 0.6
send_settle_seconds = 0.1
sip_timeout_seconds = 0.1
rate_per_hour = 2
rate_per_day = 3

[gateway gateway-a]
host = 127.0.0.1
port = {self.tg.port}
credentials_file = {credentials}
country_code = +1

[gateway gateway-b]
host = 127.0.0.1
port = {self.other_tg.port}
credentials_file = {credentials}
country_code = +44

[endpoint 2001]
gateway = gateway-a
span = 2
numbers = {PRIMARY}

[endpoint 2002]
gateway = gateway-a
span = 3
numbers = {SECONDARY}, {ALIAS}

[endpoint 2003]
gateway = gateway-b
span = 2
numbers = +447700900103
aor = other-aor
""")
        self.settings = Settings(self.config)
        self.queue = Queue(self.settings.db_path)
        self.addCleanup(self.queue.db.close)
        self.bridge = Bridge(self.settings, self.queue)
        self.peers = (self.bridge.ami, *self.bridge.gateways.values())
        self.daemon = None
        self.addAsyncCleanup(self.stop_bridge)
        await self.start_bridge()

    async def start_bridge(self):
        self.daemon = asyncio.create_task(self.bridge.run())
        await self.wait(lambda: all(peer.ready for peer in self.peers))

    async def stop_bridge(self):
        if self.daemon is not None:
            self.daemon.cancel()
            result, = await asyncio.gather(self.daemon, return_exceptions=True)
            if not isinstance(result, asyncio.CancelledError):
                raise result
            self.daemon = None

    async def wait(self, predicate, timeout=4):
        async with asyncio.timeout(timeout):
            while not predicate():
                if self.daemon is not None and self.daemon.done():
                    self.daemon.result()
                await asyncio.sleep(0.01)

    def rows(self):
        return self.queue.db.execute("SELECT * FROM queue ORDER BY id").fetchall()

    def messages(self, phone):
        return [body.decode() for _, _, body in phone.messages]

    def alerts_with(self, text):
        return self.alerts.read_text().count(text) if self.alerts.exists() else 0

    async def cli(self, *args):
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(SCRIPT), "-c", str(self.config), *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        self.assertEqual(proc.returncode, 0, err.decode())
        return out.decode()

    async def test_inbound_routes_both_spans_and_separate_gateway(self):
        self.tg.inbound(2, EXTERNAL, "BOM, plus + and Français\n中文", utf8=True)
        self.tg.inbound(3, "ExampleBrand", "brand text")
        self.other_tg.inbound(2, "67890", "short code")
        self.tg.inbound(99, EXTERNAL, "unmapped")
        self.tg.broadcast(wire(Event="ReceivedSMS", Sender=EXTERNAL))
        await self.wait(lambda: len(self.phone1.messages) == 1 and len(self.phone2.messages) == 2)
        await self.wait(lambda: not self.rows())
        _, headers, payload = self.phone1.messages[0]
        self.assertEqual(headers["From"].split(";tag=")[0], f"<sip:{EXTERNAL}@pbx.example.net>")
        self.assertEqual(headers["Content-Length"], str(len(payload)))
        self.assertEqual(payload.decode(), "BOM, plus + and Français\n中文")
        self.assertEqual(headers["Content-Type"], "text/plain;charset=UTF-8")
        self.assertEqual(set(self.messages(self.phone2)), {"brand text", "short code"})
        displays = [fields["From"] for _, fields, _ in self.phone2.messages]
        self.assertTrue(any(item.startswith('"ExampleBrand" ') for item in displays))
        self.assertTrue(any(item.startswith('"67890" ') for item in displays))

    async def test_multipart_in_order_after_restart_and_partial_timeout(self):
        self.settings.multipart_timeout = 1
        self.tg.inbound(2, EXTERNAL, "second", index=2, total=2)
        self.tg.inbound(2, EXTERNAL, "second", index=2, total=2)
        await self.wait(lambda: self.queue.db.execute("SELECT COUNT(*) FROM parts").fetchone()[0] == 1)
        await self.stop_bridge()
        await self.start_bridge()
        self.tg.inbound(2, EXTERNAL, "first ", index=1, total=2)
        await self.wait(lambda: self.messages(self.phone1) == ["first second"])
        await self.wait(lambda: not self.rows())
        self.settings.multipart_timeout = 0.1
        self.tg.inbound(2, EXTERNAL, "missing start", index=1, total=3, ref="sample-ref")
        await self.wait(lambda: INCOMPLETE + "missing start" in self.messages(self.phone1))
        await self.wait(lambda: not self.rows())
        self.assertEqual(self.queue.db.execute("SELECT COUNT(*) FROM parts").fetchone()[0], 0)

    async def test_iso8859_15_multipart_capture(self):
        first = ("Chaque été, Émilie se promène dans les rues de Paris. "
                 "Elle apprécie les cafés animés, les patisseries délicieuses et les magnifiques jardins où chantent ")
        second = "les oiseaux.\n\n161 caractères, espaces et ponctuation compris.\n+TEST"
        self.tg.inbound(2, EXTERNAL, first, index=1, total=2, ref="70")
        self.tg.inbound(2, EXTERNAL, second, index=2, total=2, ref="70")
        await self.wait(lambda: bool(self.phone1.messages) and not self.rows())
        self.assertEqual(self.messages(self.phone1), [first + second])
        self.assertEqual(self.queue.db.execute("SELECT COUNT(*) FROM parts").fetchone()[0], 0)

    async def test_inbound_euro_and_missing_emoji_captures(self):
        for content in ("Prijs+5+%A4+%5Bok%5D+%7Bx%7D+~%5E%7C",
                        "%EF%BB%BFMessage+with+%E2%82%AC+and+", "%EF%BB%BF"):
            self.tg.broadcast(wire(Event="ReceivedSMS", ID="", GsmSpan=2, Sender=EXTERNAL,
                                   Index=0, Total=1, Content=content))
        await self.wait(lambda: len(self.phone1.messages) == 3 and not self.rows())
        self.assertEqual(self.messages(self.phone1), [
            "Prijs 5 € [ok] {x} ~^|",
            "Message with € and ",
            "[The SMS contained only characters the gateway cannot pass on, such as emoji.]",
        ])

    async def test_empty_multipart_sms_gets_placeholder_after_assembly(self):
        self.tg.inbound(2, EXTERNAL, "", index=1, total=2, utf8=True)
        self.tg.inbound(2, EXTERNAL, "", index=2, total=2, utf8=True)
        await self.wait(lambda: bool(self.phone1.messages) and not self.rows())
        self.assertEqual(self.messages(self.phone1), [
            "[The SMS contained only characters the gateway cannot pass on, such as emoji.]",
        ])

    async def test_out_of_range_inbound_indices_are_rejected(self):
        with self.assertLogs("sms-bridge", level="WARNING") as logs:
            for index, total in [(0, 2), (3, 2), (1, 1)]:
                self.tg.inbound(2, EXTERNAL, "bad index", index=index, total=total)
            self.tg.inbound(2, EXTERNAL, "valid")
            await self.wait(lambda: bool(self.phone1.messages) and not self.rows())
        self.assertEqual(self.messages(self.phone1), ["valid"])
        self.assertEqual(self.queue.db.execute("SELECT COUNT(*) FROM parts").fetchone()[0], 0)
        self.assertEqual(sum("ignored malformed ReceivedSMS" in line for line in logs.output), 3)

    async def test_outbound_uses_own_span_country_and_url_encoding(self):
        body = 'Long é, with "quotes", +, %, a \\ slash, 中文\nLF\r\nCRLF\rCR\tTAB ' + 'é' * 170
        self.ami.sms_out("2001", "00 1 202 555 0199", body)
        self.ami.sms_out("2002", "0202 555 0198", "second SIM")
        self.ami.sms_out("2003", "07700 900199", "second gateway")
        await self.wait(lambda: len(self.tg.sent) == 2 and len(self.other_tg.sent) == 1)
        await self.wait(lambda: not self.rows())
        self.assertEqual(self.tg.sent, [
            (2, EXTERNAL, body.replace("\r\n", "\n").replace("\r", "\n")),
            (3, "+12025550198", "second SIM"),
        ])
        self.assertEqual(self.other_tg.sent, [(2, "+447700900199", "second gateway")])
        self.assertFalse(self.phone1.messages or self.phone2.messages)

    async def test_outbound_emoji_round_trip(self):
        body = "Emoji 👍 and 😀 + €"
        self.ami.sms_out("2001", EXTERNAL, body)
        await self.wait(lambda: bool(self.tg.sent) and not self.rows())
        self.assertEqual(self.tg.sent, [(2, EXTERNAL, body)])

    async def test_local_by_extension_and_both_aliases_with_reply(self):
        self.ami.sms_out("2001", "2002", "extension")
        self.ami.sms_out("2001", "0202 555 0102", "primary alias")
        self.ami.sms_out("2001", "00 44 7700 900102", "secondary alias\nline two")
        await self.wait(lambda: len(self.phone2.messages) == 3)
        self.assertCountEqual(self.messages(self.phone2), ["extension", "primary alias", "secondary alias\nline two"])
        self.assertTrue(all(f"<sip:{PRIMARY}@" in fields["From"] for _, fields, _ in self.phone2.messages))
        self.ami.sms_out("2002", PRIMARY, "reply")
        await self.wait(lambda: self.messages(self.phone1) == ["reply"])
        await self.wait(lambda: not self.rows())
        self.assertIn(f"<sip:{SECONDARY}@", self.phone1.messages[0][1]["From"])
        self.assertFalse(self.tg.actions)
        self.assertEqual(self.queue.db.execute("SELECT COUNT(*) FROM rate").fetchone()[0], 0)

    async def test_refusals_and_authenticated_sender_allowlist(self):
        self.ami.sms_out("unknown", EXTERNAL)
        self.ami.sms_out("unknown", EXTERNAL,
                         from_header='"lookalike <sip:2001@pbx.example.net>" <sip:unknown@pbx.example.net>')
        self.ami.sms_out("unknown", EXTERNAL,
                         from_header='"lookalike <sip:2001@2001:db8::1>" <sip:unknown@2001:db8::1>')
        self.ami.broadcast(wire(Event="UserEvent", UserEvent="SmsOut", From64="!!!"))
        self.ami.broadcast(wire(Event="UserEvent", UserEvent="Other"))
        for dest, body in [(PRIMARY, "self"), ("2001", "self"), ("ExampleBrand", "reply"),
                           ("67890", "short code reply"),
                           (EXTERNAL, ""), (EXTERNAL, "C0\x00"), (EXTERNAL, "DEL\x7f"),
                           (EXTERNAL, "C1\x85")]:
            self.ami.sms_out("2001", dest, body)
        await self.wait(lambda: len(self.phone1.messages) == 8)
        await self.wait(lambda: not self.rows())
        self.assertEqual(sum("yourself" in body for body in self.messages(self.phone1)), 2)
        self.assertEqual(sum("full telephone number" in body for body in self.messages(self.phone1)), 2)
        self.assertEqual(sum("unsupported controls" in body for body in self.messages(self.phone1)), 4)
        self.assertFalse(self.tg.actions)

    async def test_ipv6_from_host(self):
        self.ami.sms_out("2001", "2002", "IPv6 sender",
                         from_header='"Example" <sip:2001@2001:db8::1>')
        await self.wait(lambda: self.messages(self.phone2) == ["IPv6 sender"] and not self.rows())

    async def test_status_zero_is_reported_in_original_conversation_once(self):
        self.tg.status = "0"
        self.ami.contacts("2001")
        self.ami.sms_out("2001", "0012025550199", "discard private text")
        await self.wait(lambda: self.rows() and self.rows()[0]["direction"] == "report")
        self.assertNotIn("discard private text", self.rows()[0]["body"])
        self.ami.contacts("2001", self.phone1)
        await self.wait(lambda: len(self.phone1.messages) == 1)
        await self.wait(lambda: not self.rows())
        self.assertEqual(len(self.tg.actions), 1)
        self.assertIn("<sip:0012025550199@", self.phone1.messages[0][1]["From"])
        self.assertIn("reported that the SMS failed", self.messages(self.phone1)[0])

    async def test_later_failed_part_produces_failure_reply(self):
        self.tg.status = "1"
        self.settings.send_settle = 1
        self.ami.sms_out("2001", EXTERNAL, "multipart example")
        await self.wait(lambda: self.rows() and self.rows()[0]["status"] == "settling")
        self.tg.send_status(self.rows()[0]["tg_id"], "0")
        await self.wait(lambda: len(self.phone1.messages) == 1 and not self.rows())
        self.assertIn("reported that the SMS failed", self.messages(self.phone1)[0])
        self.assertEqual(len(self.tg.sent), 1)

    async def test_successful_parts_extend_settle_without_blocking_next_sms(self):
        self.settings.send_settle = 1
        self.ami.sms_out("2001", EXTERNAL, "multipart example")
        self.ami.sms_out("2002", EXTERNAL, "next SMS")
        await self.wait(lambda: len(self.tg.sent) == 2 and
                        len([row for row in self.rows() if row["status"] == "settling"]) == 2)
        first, second = self.rows()
        self.tg.send_status(first["tg_id"], "1")
        await self.wait(lambda: self.rows()[0]["due"] > first["due"])
        extended_due = self.rows()[0]["due"]
        self.bridge.tick(first["due"])
        self.assertIn(first["id"], [row["id"] for row in self.rows()])
        self.bridge.tick(max(extended_due, second["due"]))
        self.assertFalse(self.rows())
        self.assertFalse(self.phone1.messages or self.phone2.messages)

    async def test_first_status_can_arrive_after_api_timeout(self):
        self.tg.status = None
        self.settings.send_status_timeout = 2
        self.ami.sms_out("2001", EXTERNAL)
        await self.wait(lambda: len(self.tg.sent) == 1)
        await asyncio.sleep(self.settings.io_timeout + 0.05)
        row, = self.rows()
        self.assertEqual(row["status"], "submitted")
        self.tg.send_status(row["tg_id"], "1")
        await self.wait(lambda: not self.rows())
        self.assertFalse(self.phone1.messages)

    async def test_missing_status_and_crash_window_never_resubmit(self):
        self.tg.status = None
        self.tg.disconnect_after_command = True
        self.ami.sms_out("2001", EXTERNAL)
        await self.wait(lambda: len(self.tg.actions) == 1 and not self.peers[1].ready)
        self.assertEqual(self.rows()[0]["status"], "submitted")
        await self.stop_bridge()
        self.tg.disconnect_after_command = False
        await self.start_bridge()
        await self.wait(lambda: len(self.phone1.messages) == 1)
        await self.wait(lambda: not self.rows())
        self.assertIn("may have been sent", self.messages(self.phone1)[0])
        self.assertEqual(len(self.tg.actions), 1)

    async def test_unrelated_or_wrong_gateway_status_does_not_clear_row(self):
        self.settings.io_timeout = 0.6
        self.tg.status = None
        self.ami.sms_out("2001", EXTERNAL)
        await self.wait(lambda: len(self.tg.actions) == 1)
        sms_id = self.tg.actions[0]["command"].rsplit(" ", 1)[1]
        self.tg.send_status("foreign-id", "1")
        self.other_tg.send_status(sms_id, "1")
        self.tg.send_status(sms_id, "9")
        await asyncio.sleep(0.08)
        self.assertEqual(self.rows()[0]["tg_id"], sms_id)
        self.tg.send_status(sms_id, "1")
        await self.wait(lambda: not self.rows())
        self.assertFalse(self.phone1.messages)

    async def test_gateway_outage_waits_repeats_recovers_and_expires(self):
        self.settings.window = 0.5
        await self.tg.stop()
        await self.wait(lambda: not self.peers[1].ready)
        self.ami.sms_out("2001", EXTERNAL)
        await self.wait(lambda: self.rows() and self.rows()[0]["status"] == "pending")
        await self.wait(lambda: self.alerts_with("TG API gateway-a unavailable") >= 2)
        await self.wait(lambda: len(self.phone1.messages) == 1)
        self.assertIn("was not sent", self.messages(self.phone1)[0])
        self.assertFalse(self.tg.actions)
        self.settings.window = 4
        self.ami.sms_out("2001", EXTERNAL, "retry when gateway returns")
        await self.wait(lambda: any(row["direction"] == "outbound" for row in self.rows()))
        await self.tg.start()
        await self.wait(lambda: len(self.tg.actions) == 1)
        await self.wait(lambda: not self.rows() and self.alerts_with("TG API gateway-a recovered") == 1)

    async def test_failed_login_ami_outage_and_repeat_disabled(self):
        self.settings.repeat = 0
        self.settings.window = 5
        await self.ami.stop()
        self.ami.login_ok = False
        await self.ami.start()
        self.tg.inbound(2, EXTERNAL, "held while AMI is down")
        await self.wait(lambda: self.alerts_with("AMI unavailable") == 1)
        await asyncio.sleep(1.2)
        self.assertEqual(self.alerts_with("AMI unavailable"), 1)
        self.assertEqual(len(self.rows()), 1)
        self.assertFalse(self.phone1.messages)
        self.ami.login_ok = True
        await self.wait(lambda: self.alerts_with("AMI recovered") == 1)
        await self.wait(lambda: self.messages(self.phone1) == ["held while AMI is down"])
        await self.wait(lambda: not self.rows())

    async def test_ami_action_timeout_reconnects(self):
        self.settings.window = 4
        self.ami.silent = True
        self.tg.inbound(2, EXTERNAL, "held during AMI timeout")
        await self.wait(lambda: self.alerts_with("AMI unavailable") >= 1)
        self.ami.silent = False
        await self.wait(lambda: self.alerts_with("AMI recovered") == 1)
        await self.wait(lambda: not self.rows())
        self.assertEqual(self.messages(self.phone1), ["held during AMI timeout"])

    async def test_handler_errors_keep_ami_connection(self):
        receive = self.bridge.ami.receive
        failures = iter((ValueError("private text"), sqlite3.OperationalError("private text")))

        def receive_with_failures(frame):
            error = next(failures, None)
            if error is not None:
                raise error
            receive(frame)

        self.bridge.ami.receive = receive_with_failures
        writer = self.bridge.ami.writer
        with self.assertLogs("sms-bridge", level="ERROR") as logs:
            for body in ("first", "second", "third"):
                self.ami.sms_out("2001", "2002", body)
            await self.wait(lambda: self.messages(self.phone2) == ["third"] and not self.rows())
        self.assertIs(self.bridge.ami.writer, writer)
        self.assertEqual(len(logs.output), 2)
        self.assertNotIn("private text", "\n".join(logs.output))

    async def test_unreachable_contact_is_tried_last(self):
        self.ami.contacts("2001", self.phone1, self.phone2, statuses=("uNrEaChAbLe", "Reachable"))
        self.tg.inbound(2, EXTERNAL, "reachable first")
        await self.wait(lambda: self.messages(self.phone2) == ["reachable first"] and not self.rows())
        self.assertFalse(self.phone1.messages)

    async def test_sip_deliveries_to_one_endpoint_keep_queue_order(self):
        self.phone1.response = 480
        self.phone1.response_delays[b"first"] = 0.15
        self.settings.sip_timeout = 0.3
        self.settings.window = 2
        self.ami.contacts("2001", self.phone1, self.phone2)
        with self.queue.db:
            now = time.time()
            for body in ("first", "second"):
                self.queue.add("inbound", "2001", EXTERNAL, body, now, self.settings.window)
        await self.wait(lambda: len(self.phone2.messages) == 2 and not self.rows())
        self.assertEqual(self.messages(self.phone2), ["first", "second"])

    async def test_failed_sip_attempt_keeps_later_messages_waiting(self):
        self.phone1.response = 503
        self.settings.interval = 0.5
        self.settings.window = 3
        self.tg.inbound(2, EXTERNAL, "first")
        self.tg.inbound(2, EXTERNAL, "second")
        await self.wait(lambda: len(self.rows()) == 2 and self.rows()[0]["attempts"] == 1)

        self.phone1.response = 200
        first, second = self.rows()
        self.assertGreater(first["due"], second["due"])
        self.bridge.tick(second["due"])
        await self.wait(lambda: not self.rows())
        self.assertEqual(self.messages(self.phone1), ["first", "first", "second"])

    async def test_waiting_sip_row_expires_behind_in_flight_report(self):
        self.phone1.response = None
        self.settings.sip_timeout = 1
        now = time.time()
        with self.queue.db:
            self.queue.add("report", "2001", EXTERNAL, "first", now, 3)
            self.queue.add("inbound", "2001", EXTERNAL, "second", now, 0.5)
        await self.wait(lambda: self.messages(self.phone1) == ["first"])

        first, second = self.rows()
        alert = f"message {second['id']} from {EXTERNAL} could not reach 2001"
        for _ in range(2):
            self.bridge.tick(second["deadline"])
        await self.wait(lambda: self.alerts_with(alert) == 1)
        self.assertEqual([(row["id"], row["status"], row["attempts"]) for row in self.rows()],
                         [(first["id"], "pending", 0), (second["id"], "failed", 0)])

    async def test_rejected_report_expires_and_unblocks_inbound_sms(self):
        self.phone1.response = 403
        self.ami.sms_out("2001", "ExampleBrand", "reply")
        await self.wait(lambda: self.rows() and self.rows()[0]["attempts"] > 0)
        report, = self.rows()
        self.settings.window = 2
        self.tg.inbound(2, EXTERNAL, "later inbound")
        await self.wait(lambda: len(self.rows()) == 2)
        self.assertEqual(self.rows()[1]["attempts"], 0)

        alert = f"message {report['id']} to ExampleBrand could not reach 2001"
        await self.wait(lambda: self.alerts_with(alert) == 1)
        self.phone1.response = 200
        await self.wait(lambda: "later inbound" in self.messages(self.phone1) and len(self.rows()) == 1)
        listed = await self.cli("list")
        self.assertIn(f"{report['id']}\t", listed)
        self.assertEqual(self.rows()[0]["status"], "failed")
        self.assertEqual(self.alerts_with(alert), 1)
        self.assertIn("Restarted 1", await self.cli("retry", str(report["id"])))
        await self.wait(lambda: not self.rows())
        self.assertIn("full telephone number", self.messages(self.phone1)[-1])
        self.assertEqual(self.alerts_with(alert), 1)

    async def test_delivery_task_exception_is_logged_and_retried(self):
        deliver = self.bridge.deliver_sip
        attempts = 0

        async def fail_once(row):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError(row["body"])
            return await deliver(row)

        self.bridge.deliver_sip = fail_once
        self.settings.window = 3
        self.settings.interval = 0.5
        with self.assertLogs("sms-bridge", level="ERROR") as logs:
            self.tg.inbound(2, EXTERNAL, "private delivery text")
            await self.wait(lambda: self.rows() and self.rows()[0]["attempts"] == 1)
            row, = self.rows()
            self.assertGreaterEqual(row["due"], row["first_seen"] + self.settings.interval)
            self.bridge.tick(row["due"] - 0.01)
            await asyncio.sleep(0)
            self.assertEqual(attempts, 1)
            await self.wait(lambda: len(self.phone1.messages) == 1 and not self.rows())
        self.assertEqual(self.messages(self.phone1), ["private delivery text"])
        self.assertIn("SIP delivery task failed", logs.output[0])
        self.assertNotIn("private delivery text", logs.output[0])

    async def test_pending_sip_rows_for_removed_endpoint_stop_retrying(self):
        with self.assertLogs("sms-bridge", level="WARNING") as logs:
            with self.queue.db:
                now = time.time()
                self.queue.add("inbound", "removed", EXTERNAL, "queued", now, self.settings.window)
                self.queue.add("report", "removed", EXTERNAL, "failure reply", now, self.settings.window)
            self.bridge.tick(now)
            self.bridge.tick(now + self.settings.interval)
        self.assertEqual([(row["direction"], row["status"], row["attempts"]) for row in self.rows()],
                         [("inbound", "failed", 0)])
        self.assertEqual(len(logs.output), 2)
        self.assertTrue(all("no longer configured" in line for line in logs.output))

    async def test_sip_tries_other_contacts_and_ignores_mismatched_responses(self):
        self.phone1.wrong_transaction = True
        self.ami.contacts("2001", self.phone1, self.phone2)
        self.tg.inbound(2, EXTERNAL, "transaction matching")
        await self.wait(lambda: not self.rows() and bool(self.phone2.messages))
        self.assertEqual(self.messages(self.phone1), ["transaction matching"])
        self.assertEqual(self.messages(self.phone2), ["transaction matching"])

    async def test_phone_offline_then_returns_within_window(self):
        self.ami.contacts("2001")
        self.tg.inbound(2, EXTERNAL, "offline")
        await self.wait(lambda: self.rows() and self.rows()[0]["attempts"] > 0)
        self.ami.contacts("2001", self.phone1)
        await self.wait(lambda: self.messages(self.phone1) == ["offline"] and not self.rows())
        self.assertEqual(self.alerts_with("could not reach"), 0)

    async def test_delivery_failures_and_retry_cli(self):
        self.phone1.response = 480
        self.phone2.response = None
        self.tg.inbound(2, EXTERNAL, "private inbound")
        self.ami.sms_out("2001", "2002", "private local")
        await self.wait(lambda: len(list(self.queue.failed())) == 2)
        await self.wait(lambda: self.alerts_with("could not reach") == 2)
        inbound, local = self.rows()
        listed = await self.cli("list")
        self.assertIn(f"{inbound['id']}\t", listed)
        self.assertIn(f"\t2001\t{EXTERNAL}\t15", listed)
        self.assertIn(f"\t2002\t{PRIMARY}\t13", listed)
        self.assertNotIn("private", listed + self.alerts.read_text())
        sends = len(self.phone1.messages) + len(self.phone2.messages)
        await asyncio.sleep(0.15)
        self.assertEqual(sends, len(self.phone1.messages) + len(self.phone2.messages))
        self.assertIn("Restarted 1", await self.cli("retry", str(inbound["id"])))
        await self.wait(lambda: self.alerts_with("could not reach") == 3)
        self.assertEqual({row["status"] for row in self.rows()}, {"failed"})
        self.phone1.response = self.phone2.response = 202
        self.assertIn("Restarted 2", await self.cli("retry"))
        await self.wait(lambda: not self.rows())
        self.assertEqual(self.alerts_with("could not reach"), 3)
        self.assertEqual(stat.S_IMODE(os.stat(self.settings.db_path).st_mode), 0o600)
        self.assertNotIn(str(local["id"]) + "\t", await self.cli("list"))

    async def test_rate_limits_persist_and_count_hour_and_day_separately(self):
        for _ in range(3):
            self.ami.sms_out("2001", EXTERNAL)
        await self.wait(lambda: len(self.tg.actions) == 2 and len(self.phone1.messages) == 1)
        self.assertIn("rate limit", self.messages(self.phone1)[0])
        await self.stop_bridge()
        self.bridge = Bridge(self.settings, self.queue)
        self.peers = (self.bridge.ami, *self.bridge.gateways.values())
        await self.start_bridge()
        self.ami.sms_out("2001", EXTERNAL)
        await self.wait(lambda: len(self.phone1.messages) == 2)
        with self.queue.db:
            self.queue.db.execute("UPDATE rate SET time=time-3601")
        self.ami.sms_out("2001", EXTERNAL)
        self.ami.sms_out("2001", EXTERNAL)
        self.ami.sms_out("2002", EXTERNAL)
        await self.wait(lambda: len(self.tg.actions) == 4 and len(self.phone1.messages) == 3)
        with self.queue.db:
            self.queue.db.execute("UPDATE rate SET time=time-86401")
        await self.wait(lambda: self.queue.db.execute("SELECT COUNT(*) FROM rate").fetchone()[0] == 0)

    async def test_daemon_cli_and_queue_lock(self):
        await self.stop_bridge()
        await self.wait(lambda: not any(mock.connections for mock in (self.ami, self.tg, self.other_tg)))
        daemon = await asyncio.create_subprocess_exec(
            sys.executable, str(SCRIPT), "-c", str(self.config), "run",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        try:
            # The in-process connections have closed, so these new logins
            # are from the actual daemon entry point.
            await self.wait(lambda: all(mock.connections for mock in (self.ami, self.tg, self.other_tg)))
            duplicate = await asyncio.create_subprocess_exec(
                sys.executable, str(SCRIPT), "-c", str(self.config),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            _, err = await duplicate.communicate()
            self.assertEqual(duplicate.returncode, 1)
            self.assertIn(b"another daemon is using this queue", err)
            self.tg.inbound(2, EXTERNAL, "private subprocess body")
            await self.wait(lambda: self.messages(self.phone1) == ["private subprocess body"] and not self.rows())
        finally:
            daemon.terminate()
            _, err = await daemon.communicate()
        self.assertEqual(daemon.returncode, 0, err.decode())
        self.assertNotIn(b"private subprocess body", err)


    async def test_disabled_hook_still_marks_failed(self):
        self.settings.hook = ""
        self.ami.contacts("2001")
        with self.assertLogs("sms-bridge", level="INFO") as logs:
            self.tg.inbound(2, EXTERNAL, "private text")
            await self.wait(lambda: len(list(self.queue.failed())) == 1)
        self.assertFalse(self.alerts.exists())
        self.assertIn("could not reach", "\n".join(logs.output))
        self.assertNotIn("private text", "\n".join(logs.output))


class ExampleTests(unittest.TestCase):
    def test_ami_write_permission(self):
        manager = configparser.ConfigParser()
        manager.read(SCRIPT.parent / "examples" / "manager.conf")
        self.assertEqual(manager["bridge-account"]["write"], "reporting")


class NumberTests(unittest.TestCase):
    def test_normalization(self):
        for value in ("+1 202 555 0101", "00 1 202 555 0101", "0202 555 0101"):
            self.assertEqual(normalize(value, "+1"), PRIMARY)
        for value in ("not-a-number", "67890", "+0000000", "2025550199", "+1202555019999999"):
            self.assertIsNone(normalize(value, "+1"))
