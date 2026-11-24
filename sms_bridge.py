#!/usr/bin/env python3
"""Bridge TG-series SMS and SIP MESSAGE. See README.md for configuration."""
import argparse
import asyncio
import base64
import configparser
import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import ipaddress
import logging
import math
import os
from pathlib import Path
import re
import signal
import socket
import sqlite3
import subprocess
import time
from urllib.parse import quote, unquote_plus
from uuid import uuid4


LOG = logging.getLogger("sms-bridge")
NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")
NUMBER = re.compile(r"\+[1-9][0-9]{6,14}")
# PJSIP contact names are <aor>;@<hash> for dynamic contacts.
CONTACT_NAME = re.compile(r"[A-Za-z0-9_.;@-]{1,256}")
HOST = r"(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])"
CONTACT_URI = re.compile(
    rf"sip:(?:[^\s<>@]+@)?(?P<host>{HOST})(?::(?P<port>[0-9]{{1,5}}))?"
    r"(?P<params>;[^\s<>]*)?", re.ASCII | re.IGNORECASE
)
MAX_FRAME = 256 * 1024
FROM_USER = re.compile(
    rf"<sips?:([A-Za-z0-9_.-]{{1,64}})@(?:{HOST}|[0-9A-Fa-f:]*:[0-9A-Fa-f:.]+)>\Z",
    re.ASCII,
)


def normalize(value, country):
    value = "".join(value.split())
    if value.startswith("00"):
        value = "+" + value[2:]
    elif value.startswith("0"):
        value = country + value[1:]
    return value if NUMBER.fullmatch(value) else None


def token(value):
    if not NAME.fullmatch(value):
        raise ValueError("invalid identifier")
    return value


def credentials(path):
    user, separator, secret = Path(path).read_text(encoding="utf-8").rstrip("\r\n").partition(":")
    if not separator or not user.strip() or not secret.strip() or any(
        ord(char) < 32 or ord(char) == 127 for char in user + secret
    ):
        raise ValueError("invalid credentials file")
    return user, secret


@dataclass(frozen=True)
class Peer:
    host: str
    port: int
    credentials_file: str


@dataclass(frozen=True)
class Gateway:
    peer: Peer
    country: str


@dataclass(frozen=True)
class Endpoint:
    gateway: str
    span: int
    numbers: tuple
    aor: str


class Settings:
    def __init__(self, path):
        config = configparser.ConfigParser(interpolation=None)
        with open(path, encoding="utf-8") as source:
            config.read_file(source)
        bridge = config["bridge"]
        self.db_path = bridge.get("db_path", "/var/lib/sms-bridge/queue.sqlite3")
        self.ami = Peer("127.0.0.1", self.port(bridge, "ami_port"), bridge["ami_credentials_file"])
        self.domain = bridge["sip_domain"]
        if not re.fullmatch(rf"{HOST}(?::[0-9]{{1,5}})?", self.domain):
            raise ValueError("invalid SIP domain")
        self.bind = bridge.get("sip_bind", "")
        if self.bind:
            ipaddress.ip_address(self.bind)
        self.hook = bridge.get("notify_hook", "/usr/local/bin/pushover.sh")
        self.repeat = self.seconds(bridge, "repeat_hours", 6, scale=3600, zero=True)
        self.window = self.seconds(bridge, "retry_hours", 6, scale=3600)
        self.interval = self.seconds(bridge, "retry_seconds", 60)
        self.multipart_timeout = self.seconds(bridge, "multipart_seconds", 120)
        self.io_timeout = self.seconds(bridge, "api_timeout_seconds", 10)
        self.send_status_timeout = self.seconds(bridge, "send_status_seconds", 120)
        self.send_settle = self.seconds(bridge, "send_settle_seconds", 30)
        self.sip_timeout = self.seconds(bridge, "sip_timeout_seconds", 8)
        self.hourly = bridge.getint("rate_per_hour", 20)
        self.daily = bridge.getint("rate_per_day", 100)
        if min(self.hourly, self.daily) <= 0:
            raise ValueError("rate limits must be positive")
        self.gateways = {}
        self.endpoints = {}
        self.aliases = {}
        self.spans = {}
        for section in config:
            if section in ("DEFAULT", "bridge"):
                continue
            kind, _, name = section.partition(" ")
            token(name)
            item = config[section]
            if kind == "gateway":
                country = item["country_code"]
                if not re.fullmatch(r"\+[1-9][0-9]{0,2}", country):
                    raise ValueError("invalid country code")
                self.gateways[name] = Gateway(
                    Peer(item["host"], self.port(item, "port"), item["credentials_file"]), country
                )
            elif kind == "endpoint":
                numbers = tuple(item["numbers"].replace(",", " ").split())
                span = item.getint("span")
                gateway = token(item["gateway"])
                if span < 2 or (gateway, span) in self.spans or not numbers:
                    raise ValueError("invalid or duplicate span, or missing numbers")
                for number in numbers:
                    if not NUMBER.fullmatch(number) or number in self.aliases:
                        raise ValueError("invalid or duplicate alias")
                    self.aliases[number] = name
                self.endpoints[name] = Endpoint(gateway, span, numbers, token(item.get("aor", name)))
                self.spans[gateway, span] = name
            else:
                raise ValueError("unknown section")
        if not self.endpoints or any(e.gateway not in self.gateways for e in self.endpoints.values()):
            raise ValueError("configure endpoints and their gateways")

    @staticmethod
    def port(section, key):
        port = section.getint(key, 5038)
        if not 1 <= port <= 65535:
            raise ValueError("invalid port")
        return port

    @staticmethod
    def seconds(section, key, default, scale=1, zero=False):
        value = section.getfloat(key, default) * scale
        if not math.isfinite(value) or value < 0 or (value == 0 and not zero):
            raise ValueError("invalid timing setting")
        return value


class Queue:
    def __init__(self, path, create=True):
        if create:
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            os.chmod(path, 0o600)
        # The CLI opens an existing queue only, so root cannot accidentally
        # create state that the service user cannot use.
        uri = Path(path).absolute().as_uri() + "?mode=rw"
        self.db = sqlite3.connect(uri, uri=True, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA secure_delete=ON")
        if not create:
            return
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                direction TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                number TEXT NOT NULL,
                body TEXT NOT NULL,
                first_seen REAL NOT NULL,
                deadline REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                due REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                tg_id TEXT,
                reply_from TEXT
            );
            CREATE INDEX IF NOT EXISTS queue_due ON queue(status, due);
            CREATE UNIQUE INDEX IF NOT EXISTS queue_tg_id ON queue(tg_id);
            CREATE TABLE IF NOT EXISTS rate (endpoint TEXT NOT NULL, time REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS rate_endpoint ON rate(endpoint, time);
            CREATE INDEX IF NOT EXISTS rate_time ON rate(time);
            CREATE TABLE IF NOT EXISTS parts (
                gateway TEXT NOT NULL, span INTEGER NOT NULL, sender TEXT NOT NULL,
                reference TEXT NOT NULL, total INTEGER NOT NULL, part INTEGER NOT NULL,
                body TEXT NOT NULL, received REAL NOT NULL,
                PRIMARY KEY (gateway, span, sender, reference, total, part)
            );
            CREATE INDEX IF NOT EXISTS parts_received ON parts(received);
            CREATE TABLE IF NOT EXISTS connection_fault (
                peer TEXT PRIMARY KEY, notified REAL NOT NULL
            );
        """)

    def add(self, direction, endpoint, number, body, now, window, reply_from=None):
        row = self.db.execute(
            """INSERT INTO queue (direction, endpoint, number, body, first_seen, deadline, due, reply_from)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (direction, endpoint, number, body, now, now + window, now, reply_from),
        )
        LOG.info("queued id=%d direction=%s endpoint=%s number=%r length=%d",
                 row.lastrowid, direction, endpoint, number, len(body))

    def retry(self, now, window, message_id):
        with self.db:
            return self.db.execute(
                """UPDATE queue SET status='pending', attempts=0, due=?, deadline=?
                   WHERE status='failed' AND (? IS NULL OR id=?)""",
                (now, now + window, message_id, message_id),
            ).rowcount

    def failed(self):
        return self.db.execute(
            "SELECT id, first_seen, endpoint, number, length(body) AS length FROM queue "
            "WHERE status='failed' ORDER BY id"
        )

    def delete(self, row):
        self.db.execute("DELETE FROM queue WHERE id=?", (row["id"],))
        LOG.info("delivered id=%d direction=%s endpoint=%s number=%r length=%d",
                 row["id"], row["direction"], row["endpoint"], row["number"], len(row["body"]))

    def report(self, row, reason, now, window):
        self.db.execute(
            """UPDATE queue SET direction='report', status='pending', body=?, due=?,
               deadline=?, attempts=0, tg_id=NULL WHERE id=?""",
            (reason, now, now + window, row["id"]),
        )
        LOG.info("outbound failed id=%d endpoint=%s number=%r reason=%s length=%d",
                 row["id"], row["endpoint"], row["number"], reason, len(row["body"]))


async def read_frame(reader):
    raw = await reader.readuntil(b"\r\n\r\n")
    fields = {}
    for line in raw.decode("utf-8", errors="replace").split("\r\n"):
        key, separator, value = line.partition(":")
        if separator:
            fields.setdefault(key.lower(), value.lstrip(" "))
    return fields


def encode_frame(fields):
    if any("\r" in value or "\n" in value or "\0" in value for value in fields.values()):
        raise ValueError("invalid wire value")
    return ("\r\n".join(f"{key}: {value}" for key, value in fields.items()) + "\r\n\r\n").encode()


class Manager:
    """One reader per connection. AMI replies use ActionID; TG events use SMS ID."""

    def __init__(self, peer, timeout, receive, name):
        self.peer = peer
        self.timeout = timeout
        self.receive = receive
        self.name = name
        self.writer = None
        self.ready = False
        self.waiting = {}

    async def action(self, action, **fields):
        if not self.ready:
            raise ConnectionError
        writer = self.writer
        action_id = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.waiting[action_id] = future
        try:
            writer.write(encode_frame({"Action": action, "ActionID": action_id, **fields}))
            return await asyncio.wait_for(future, self.timeout)
        except TimeoutError:
            writer.close()
            raise
        finally:
            self.waiting.pop(action_id, None)

    async def getvar(self, variable):
        result = await self.action("Getvar", Variable=variable)
        if result.get("response") == "Success":
            return result.get("value", "")
        return ""

    async def connect(self):
        reader, self.writer = await asyncio.open_connection(
            self.peer.host, self.peer.port, limit=MAX_FRAME
        )
        sock = self.writer.get_extra_info("socket")
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
        if not (await reader.readline()).startswith(b"Asterisk Call Manager/"):
            raise ConnectionError
        user, secret = credentials(self.peer.credentials_file)
        self.writer.write(encode_frame({"Action": "Login", "Username": user, "Secret": secret}))
        while True:
            frame = await read_frame(reader)
            if "response" in frame:
                if frame["response"] != "Success":
                    raise ConnectionError
                return reader

    async def run(self):
        delay = 1
        while True:
            try:
                reader = await asyncio.wait_for(self.connect(), self.timeout)
                self.ready = True
                delay = 1
                while True:
                    frame = await read_frame(reader)
                    future = self.waiting.get(frame.get("actionid"))
                    if "response" in frame and future is not None and not future.done():
                        future.set_result(frame)
                    elif "event" in frame:
                        try:
                            self.receive(frame)
                        except sqlite3.Error:
                            LOG.error("%s event handler failed with a database error", self.name)
                        except Exception:
                            LOG.error("%s event handler failed", self.name)
            except (OSError, ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                LOG.debug("%s connection closed or unavailable", self.name)
            finally:
                self.ready = False
                if self.writer:
                    self.writer.close()
                    self.writer = None
                for future in self.waiting.values():
                    if not future.done():
                        future.set_exception(ConnectionError())
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


def sip_address(sockaddr):
    host, port = sockaddr[:2]
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def sender_uri(sender, domain):
    user = quote(sender, safe="+-._~")
    display = "" if re.fullmatch(r"\+?[0-9]{7,17}", sender) else sender
    if display:
        display = '"' + display.replace("\\", "\\\\").replace('"', '\\"') + '" '
    return f"{display}<sip:{user}@{domain}>"


def sip_response(data, call_id, branch):
    headers = data.split(b"\r\n\r\n", 1)[0].decode("ascii", errors="replace").split("\r\n")
    status = re.fullmatch(r"SIP/2\.0 ([1-6][0-9]{2})(?: .*)?", headers[0])
    fields = {}
    for line in headers[1:]:
        key, sep, value = line.partition(":")
        if sep:
            fields.setdefault(key.lower(), value.strip())
    via = fields.get("via", fields.get("v", ""))
    branches = re.findall(r"(?:^|;)\s*branch=([^;,\s]+)", via.split(",", 1)[0], re.I)
    if (status and fields.get("call-id", fields.get("i")) == call_id
            and fields.get("cseq", "").split() == ["1", "MESSAGE"] and branches == [branch]):
        return int(status[1])
    return 0


async def send_sip(settings, contact, endpoint, sender, body):
    # Asterisk 22.12.0 MessageSend (dialplan and AMI) reports that the request
    # was sent. The phone's response reaches only msg_response_callback() in
    # res_pjsip_messaging.c (debug logging), never the dialplan or AMI. An
    # asleep, disconnected or crashed phone could therefore lose a message.
    # Send directly so the queue only clears after the phone returns 2xx.
    match = CONTACT_URI.fullmatch(contact)
    if not match or not contact.isascii():
        return False
    params = (match["params"] or "").lower().split(";")
    if any(p.startswith("maddr=") or (p.startswith("transport=") and p != "transport=udp")
           for p in params):
        return False
    host = match["host"].strip("[]")
    port = int(match["port"] or 5060)
    if not 1 <= port <= 65535:
        return False
    loop = asyncio.get_running_loop()
    family = socket.AF_UNSPEC
    if settings.bind:
        family = socket.AF_INET6 if ":" in settings.bind else socket.AF_INET
    try:
        async with asyncio.timeout(settings.sip_timeout):
            addresses = await loop.getaddrinfo(host, port, family=family, type=socket.SOCK_DGRAM)
            family, sock_type, proto, _, address = addresses[0]
            with socket.socket(family, sock_type, proto) as sock:
                sock.setblocking(False)
                if settings.bind:
                    sock.bind((settings.bind, 0))
                await loop.sock_connect(sock, address)
                call_id = uuid4().hex + "@" + re.sub(r":[0-9]+$", "", settings.domain)
                branch = "z9hG4bK" + uuid4().hex
                payload = body.encode()
                request = (
                    f"MESSAGE {contact} SIP/2.0\r\n"
                    f"Via: SIP/2.0/UDP {sip_address(sock.getsockname())};branch={branch};rport\r\n"
                    "Max-Forwards: 70\r\n"
                    f"From: {sender_uri(sender, settings.domain)};tag={uuid4().hex}\r\n"
                    f"To: <sip:{endpoint}@{settings.domain}>\r\n"
                    f"Call-ID: {call_id}\r\n"
                    "CSeq: 1 MESSAGE\r\n"
                    "Content-Type: text/plain;charset=UTF-8\r\n"
                    f"Content-Length: {len(payload)}\r\n\r\n"
                ).encode() + payload
                # UDP retransmissions reuse the transaction ID.
                interval = 0.5
                while True:
                    await loop.sock_sendall(sock, request)
                    until = loop.time() + interval
                    try:
                        async with asyncio.timeout_at(until):
                            while True:
                                response = await loop.sock_recv(sock, 65535)
                                status = sip_response(response, call_id, branch)
                                if status >= 200:
                                    return status < 300
                                if status >= 100:
                                    interval = 4
                    except TimeoutError:
                        interval = min(interval * 2, 4)
    except OSError:
        return False


UNKNOWN_SMS = "The gateway did not confirm the SMS result. It may have been sent."
INCOMPLETE = "[Incomplete SMS] "
EMPTY_SMS = "[The SMS contained only characters the gateway cannot pass on, such as emoji.]"


class Bridge:
    def __init__(self, settings, queue):
        self.settings = settings
        self.queue = queue
        self.db = queue.db
        self.ami = Manager(settings.ami, settings.io_timeout, self.outbound, "AMI")
        self.gateways = {
            name: Manager(gateway.peer, settings.io_timeout,
                          lambda frame, name=name: self.from_gateway(name, frame),
                          f"TG API {name}")
            for name, gateway in settings.gateways.items()
        }
        self.alerts = set()
        self.deliveries = {}

    def outbound(self, frame):
        if frame.get("event") != "UserEvent" or frame.get("userevent") != "SmsOut":
            return
        try:
            # Asterisk's MESSAGE(from) ends with the authenticated SIP URI.
            # Only parse that suffix. The display name can contain lookalike URIs.
            sender = FROM_USER.search(self.decode(frame, "from64"))
            if sender is None:
                raise ValueError
            endpoint = sender[1]
            destination = self.decode(frame, "destination64")
            body = self.decode(frame, "body64")
            if not self.valid_address(destination):
                raise ValueError
        except (KeyError, ValueError):
            LOG.warning("ignored malformed SmsOut event")
            return
        if endpoint not in self.settings.endpoints:
            LOG.warning("ignored SmsOut event from an unconfigured endpoint")
            return
        sender = self.settings.endpoints[endpoint]
        country = self.settings.gateways[sender.gateway].country
        number = normalize(destination, country)
        target = destination if destination in self.settings.endpoints else self.settings.aliases.get(number)
        now = time.time()
        window = self.settings.window
        sms_body = body.replace("\r\n", "\n").replace("\r", "\n")
        with self.db:
            if target == endpoint:
                self.refuse(endpoint, destination, "You cannot send a message to yourself.", now)
            elif target:
                self.queue.add("local", target, sender.numbers[0], body, now, window)
            elif not number:
                self.refuse(endpoint, destination, "SMS requires a full telephone number.", now)
            elif not sms_body or any(
                char not in "\n\t" and (ord(char) < 32 or 127 <= ord(char) <= 159) for char in sms_body
            ):
                self.refuse(endpoint, destination, "SMS requires non-empty text without unsupported controls.", now)
            elif not self.admit_sms(endpoint, now):
                self.refuse(endpoint, destination, "SMS rate limit exceeded.", now)
            else:
                self.queue.add("outbound", endpoint, number, sms_body, now, window, destination)

    @staticmethod
    def decode(frame, key):
        return base64.b64decode(frame[key], validate=True).decode("utf-8")

    @staticmethod
    def valid_address(value):
        return 0 < len(value) <= 128 and value.isprintable() and bool(value.strip())

    def refuse(self, endpoint, destination, reason, now):
        LOG.info("refused endpoint=%s number=%r reason=%s", endpoint, destination, reason)
        self.queue.add("report", endpoint, destination, reason, now, self.settings.window)

    def admit_sms(self, endpoint, now):
        hour, day = self.db.execute(
            """SELECT COUNT(CASE WHEN time>? THEN 1 END), COUNT(*) FROM rate
               WHERE endpoint=? AND time>?""",
            (now - 3600, endpoint, now - 86400),
        ).fetchone()
        if hour >= self.settings.hourly or day >= self.settings.daily:
            return False
        self.db.execute("INSERT INTO rate VALUES (?, ?)", (endpoint, now))
        return True

    def from_gateway(self, gateway, frame):
        event = frame.get("event")
        if event == "ReceivedSMS":
            self.receive_sms(gateway, frame)
        elif event == "UpdateSMSSend" and frame.get("status") in ("0", "1"):
            with self.db:
                row = self.db.execute(
                    "SELECT * FROM queue WHERE status IN ('submitted', 'settling') AND tg_id=?",
                    (frame.get("id"),),
                ).fetchone()
                if row is None:
                    return
                endpoint = self.settings.endpoints.get(row["endpoint"])
                if endpoint and endpoint.gateway == gateway:
                    now = time.time()
                    if row["status"] == "submitted" and now >= row["due"]:
                        return
                    if frame["status"] == "0":
                        self.queue.report(row, "The gateway reported that the SMS failed.",
                                          now, self.settings.window)
                    else:
                        self.db.execute(
                            "UPDATE queue SET status='settling', due=? WHERE id=?",
                            (now + self.settings.send_settle, row["id"]),
                        )

    def receive_sms(self, gateway, frame):
        try:
            span = int(frame["gsmspan"])
            index, total = int(frame["index"]), int(frame["total"])
            sender = frame["sender"].strip()
            if total == 1:
                valid_index = index == 0
            else:
                valid_index = 1 <= index <= total <= 255
                index -= 1
            if not valid_index or not self.valid_address(sender):
                raise ValueError
            content = frame["content"]
            if content[:9].lower() == "%ef%bb%bf":
                body = unquote_plus(content[9:], encoding="utf-8", errors="strict")
            else:
                body = unquote_plus(content, encoding="iso8859_15")
        except (KeyError, ValueError):
            LOG.warning("ignored malformed ReceivedSMS event from %s", gateway)
            return
        endpoint = self.settings.spans.get((gateway, span))
        if not endpoint:
            LOG.warning("ignored ReceivedSMS from unmapped gateway=%s span=%d", gateway, span)
            return
        sender = normalize(sender, self.settings.gateways[gateway].country) or sender
        now = time.time()
        with self.db:
            if total == 1:
                self.queue.add("inbound", endpoint, sender, body or EMPTY_SMS, now, self.settings.window)
                return
            # Older firmware supplies an empty ID. Use the available metadata
            # and handle a conflicting index as the start of a separate SMS.
            reference = frame.get("id") or frame.get("smsc", "")
            key = gateway, span, sender, reference, total
            parts = self.parts(key)
            expired = parts and now - min(row["received"] for row in parts) >= self.settings.multipart_timeout
            conflicting = any(row["part"] == index and row["body"] != body for row in parts)
            if expired or conflicting:
                self.finish_parts(key, parts, now)
                parts = []
            if any(row["part"] == index for row in parts):
                return
            self.db.execute("INSERT INTO parts VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (*key, index, body, now))
            if len(parts) + 1 == total:
                self.finish_parts(key, self.parts(key), now)

    def parts(self, key):
        return self.db.execute(
            """SELECT part, body, received FROM parts WHERE
               gateway=? AND span=? AND sender=? AND reference=? AND total=? ORDER BY part""", key
        ).fetchall()

    def finish_parts(self, key, parts, now):
        gateway, span, sender, _, total = key
        endpoint = self.settings.spans.get((gateway, span))
        if endpoint:
            body = "".join(row["body"] for row in parts) or EMPTY_SMS
            if len(parts) != total:
                body = INCOMPLETE + body
            self.queue.add("inbound", endpoint, sender, body, now, self.settings.window)
        self.db.execute(
            "DELETE FROM parts WHERE gateway=? AND span=? AND sender=? AND reference=? AND total=?", key
        )

    def notify(self, message):
        LOG.warning("%s", message)
        if self.settings.hook:
            task = asyncio.create_task(asyncio.to_thread(self.run_hook, message))
            self.alerts.add(task)
            task.add_done_callback(self.alerts.discard)

    def run_hook(self, message):
        try:
            subprocess.run([self.settings.hook, message], check=True, timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            LOG.warning("notify_hook failed")

    def connection_notifications(self, now):
        for peer in [self.ami, *self.gateways.values()]:
            with self.db:
                row = self.db.execute(
                    "SELECT notified FROM connection_fault WHERE peer=?", (peer.name,)
                ).fetchone()
                if peer.ready and row:
                    self.db.execute("DELETE FROM connection_fault WHERE peer=?", (peer.name,))
                    message = f"SMS bridge: {peer.name} recovered"
                elif not peer.ready and (row is None or
                                         self.settings.repeat and now - row[0] >= self.settings.repeat):
                    self.db.execute("REPLACE INTO connection_fault VALUES (?, ?)", (peer.name, now))
                    message = f"SMS bridge: {peer.name} unavailable"
                else:
                    continue
            self.notify(message)

    def start_sms(self, peer, row, span, now):
        tg_id = str(1 + uuid4().int % 2147483646)
        # The gateway decodes up to three UTF-8 bytes at a time, so encode
        # each UTF-16 code unit separately to preserve supplementary characters.
        utf16 = row["body"].encode("utf-16-le")
        units = "".join(chr(low | (high << 8)) for low, high in zip(utf16[::2], utf16[1::2]))
        text = quote(units, safe="", errors="surrogatepass")
        command = f'gsm send sms {span} {row["number"]} "{text}" {tg_id}'
        packet = encode_frame({"Action": "smscommand", "command": command})
        # Commit before writing so reconnect/restart cannot cause duplicate
        # charged SMS. Only work that was still pending can be retried.
        with self.db:
            self.db.execute(
                """UPDATE queue SET status='submitted', tg_id=?, due=?, attempts=attempts+1 WHERE id=?""",
                (tg_id, now + self.settings.send_status_timeout, row["id"]),
            )
        try:
            peer.writer.write(packet)
        except OSError:
            peer.writer.close()
        LOG.info("submitted id=%d endpoint=%s number=%r length=%d",
                 row["id"], row["endpoint"], row["number"], len(row["body"]))

    async def deliver_sip(self, row):
        endpoint = self.settings.endpoints[row["endpoint"]]
        sender = row["reply_from"] or row["number"]
        try:
            names = await self.ami.getvar(f"PJSIP_AOR({endpoint.aor},contact)")
            contacts = []
            for name in dict.fromkeys(re.split(r"[,\s]+", names)):
                if not CONTACT_NAME.fullmatch(name):
                    continue
                uri = await self.ami.getvar(f"PJSIP_CONTACT({name},uri)")
                status = await self.ami.getvar(f"PJSIP_CONTACT({name},status)")
                if uri:
                    contacts.append((status.lower() in ("unreachable", "unavailable", "unavail"), uri))
            for _, uri in sorted(contacts, key=lambda contact: contact[0]):
                if await send_sip(self.settings, uri, row["endpoint"], sender, row["body"]):
                    return True
        except OSError:
            return False
        return False

    async def deliver_and_update(self, row):
        succeeded = await self.deliver_sip(row)
        with self.db:
            if succeeded:
                self.queue.delete(row)
            else:
                self.reschedule(row, time.time())
        if not succeeded:
            LOG.info("SIP attempt failed id=%d direction=%s endpoint=%s number=%r length=%d",
                     row["id"], row["direction"], row["endpoint"], row["number"], len(row["body"]))

    def reschedule(self, row, now):
        due = min(now + self.settings.interval, row["deadline"])
        self.db.execute("UPDATE queue SET due=?, attempts=attempts+1 WHERE id=?", (due, row["id"]))

    def tick(self, now):
        self.connection_notifications(now)
        with self.db:
            self.db.execute("DELETE FROM rate WHERE time<=?", (now - 86400,))
            for key in self.db.execute(
                """SELECT DISTINCT gateway, span, sender, reference, total FROM parts WHERE received<=?""",
                (now - self.settings.multipart_timeout,),
            ).fetchall():
                key = tuple(key)
                self.finish_parts(key, self.parts(key), now)
            for row in self.db.execute(
                "SELECT * FROM queue WHERE status IN ('submitted', 'settling') AND due<=?", (now,)
            ).fetchall():
                if row["status"] == "settling":
                    self.queue.delete(row)
                else:
                    self.queue.report(row, UNKNOWN_SMS, now, self.settings.window)
        for endpoint_name, (message_id, task) in list(self.deliveries.items()):
            if task.done():
                try:
                    task.result()
                except (Exception, asyncio.CancelledError) as error:
                    # Keep the traceback without exception text that could contain message data.
                    safe_error = RuntimeError(type(error).__name__).with_traceback(error.__traceback__)
                    LOG.exception("SIP delivery task failed id=%d endpoint=%s", message_id, endpoint_name,
                                  exc_info=safe_error)
                    with self.db:
                        row = self.db.execute(
                            "SELECT id, deadline FROM queue WHERE id=? AND status='pending'", (message_id,)
                        ).fetchone()
                        if row is not None:
                            self.reschedule(row, now)
                del self.deliveries[endpoint_name]
        busy = {
            endpoint.gateway for row in self.db.execute("SELECT endpoint FROM queue WHERE status='submitted'")
            if (endpoint := self.settings.endpoints.get(row["endpoint"]))
        }
        # Find each SIP queue's head before filtering by due time.
        for row in self.db.execute(
            """WITH sip_heads AS (
                   SELECT endpoint, MIN(id) AS id FROM queue
                   WHERE status='pending' AND direction<>'outbound' GROUP BY endpoint
               )
               SELECT queue.*, sip_heads.id AS sip_head_id FROM queue
               LEFT JOIN sip_heads USING (endpoint)
               WHERE status='pending' AND
                   (due<=? OR (direction<>'outbound' AND deadline<=?))
               ORDER BY due, queue.id""", (now, now)
        ).fetchall():
            direction = row["direction"]
            endpoint_name = row["endpoint"]
            if direction != "outbound":
                delivery = self.deliveries.get(endpoint_name)
                if delivery and delivery[0] == row["id"]:
                    continue
                if endpoint_name not in self.settings.endpoints:
                    with self.db:
                        if direction == "report":
                            self.db.execute("DELETE FROM queue WHERE id=?", (row["id"],))
                        else:
                            self.db.execute("UPDATE queue SET status='failed' WHERE id=?", (row["id"],))
                    LOG.warning("stopped SIP delivery id=%d direction=%s endpoint=%s no longer configured",
                                row["id"], direction, endpoint_name)
                    continue
            if now >= row["deadline"]:
                if direction == "outbound":
                    with self.db:
                        self.queue.report(row, "The SMS was not sent before the gateway retry window expired.",
                                          now, self.settings.window)
                else:
                    with self.db:
                        self.db.execute("UPDATE queue SET status='failed' WHERE id=?", (row["id"],))
                    # A report's number is the SMS destination, not a sender.
                    relation = "to" if direction == "report" else "from"
                    self.notify(f"SMS bridge: message {row['id']} {relation} {row['number']} "
                                f"could not reach {row['endpoint']}")
            elif direction == "outbound":
                endpoint = self.settings.endpoints.get(endpoint_name)
                if endpoint is None:
                    with self.db:
                        self.queue.report(row, "The sending endpoint is no longer configured.",
                                          now, self.settings.window)
                    continue
                peer = self.gateways[endpoint.gateway]
                if peer.ready and endpoint.gateway not in busy:
                    self.start_sms(peer, row, endpoint.span, now)
                    busy.add(endpoint.gateway)
                elif not peer.ready:
                    with self.db:
                        self.reschedule(row, now)
            elif endpoint_name in self.deliveries or row["id"] != row["sip_head_id"]:
                continue
            elif self.ami.ready and len(self.deliveries) < 16:
                task = asyncio.create_task(self.deliver_and_update(row))
                self.deliveries[endpoint_name] = row["id"], task
            elif not self.ami.ready:
                with self.db:
                    self.reschedule(row, now)

    async def run(self):
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        peers = [asyncio.create_task(peer.run()) for peer in [self.ami, *self.gateways.values()]]
        try:
            # Allow the initial login to finish before declaring an outage.
            started = loop.time()
            while not stop.is_set():
                for task in peers:
                    if task.done():
                        task.result()
                if loop.time() - started >= self.settings.io_timeout or all(
                    peer.ready for peer in [self.ami, *self.gateways.values()]
                ):
                    self.tick(time.time())
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), min(1, self.settings.interval))
        finally:
            tasks = [*peers, *(task for _, task in self.deliveries.values())]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, *self.alerts, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-c", "--config", default="/etc/sms-bridge/bridge.conf")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("run", help="run the bridge daemon (default)")
    commands.add_parser("list", help="list failed SIP deliveries without message text")
    retry = commands.add_parser("retry", help="restart delivery of failed SIP messages")
    retry.add_argument("id", type=int, nargs="?")
    args = parser.parse_args()
    if args.command == "retry" and args.id is not None and not 0 < args.id < 2**63:
        parser.error("id must be a positive SQLite row ID")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.umask(0o077)
    try:
        settings = Settings(args.config)
        daemon = args.command in (None, "run")
        with contextlib.ExitStack() as stack:
            if daemon:
                lock = stack.enter_context(open(settings.db_path + ".lock", "a", encoding="utf-8"))
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    LOG.error("another daemon is using this queue")
                    return 1
            queue = Queue(settings.db_path, create=daemon)
            stack.callback(queue.db.close)
            if daemon:
                asyncio.run(Bridge(settings, queue).run())
            elif args.command == "list":
                print("ID\tRECEIVED (UTC)\tENDPOINT\tSENDER\tLENGTH")
                for row in queue.failed():
                    received = datetime.fromtimestamp(row["first_seen"], timezone.utc).isoformat(timespec="seconds")
                    print(row["id"], received, row["endpoint"], row["number"], row["length"], sep="\t")
            else:
                print(f"Restarted {queue.retry(time.time(), settings.window, args.id)} message(s)")
    except (OSError, ValueError, KeyError, configparser.Error, sqlite3.Error) as error:
        # Parser and SQLite errors can contain configuration or message data.
        LOG.error("bridge failed (%s); check configuration and queue access", type(error).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
