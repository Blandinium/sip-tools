"""Loopback peers for exercising the bridge's real TCP and UDP paths."""
import asyncio
import base64
from urllib.parse import quote_plus, unquote_plus


LOOPBACK = "127.0.0.1"


def wire(**fields):
    return ("\r\n".join(f"{key}: {value}" for key, value in fields.items()) + "\r\n\r\n").encode()


class MockManager:
    def __init__(self):
        self.connections = set()
        self.sessions = set()
        self.server = None
        self.port = 0
        self.login_ok = True
        self.actions = []

    async def start(self):
        self.server = await asyncio.start_server(self.session, LOOPBACK, self.port, reuse_address=True)
        self.port = self.server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self):
        self.server.close()
        for writer in list(self.connections):
            writer.close()
        await self.server.wait_closed()
        await asyncio.gather(*list(self.sessions))

    async def session(self, reader, writer):
        task = asyncio.current_task()
        self.sessions.add(task)
        self.connections.add(writer)
        try:
            writer.write(b"Asterisk Call Manager/1.1\r\n")
            while True:
                raw = await reader.readuntil(b"\r\n\r\n")
                fields = dict(line.split(": ", 1) for line in raw.decode().splitlines() if line)
                if fields["Action"] == "Login":
                    accepted = self.login_ok and (fields["Username"], fields["Secret"]) == (
                        "test-account", "test-placeholder"
                    )
                    writer.write(wire(Response="Success" if accepted else "Error",
                                      Message="Authentication accepted" if accepted else "Authentication failed"))
                    if not accepted:
                        break
                else:
                    self.actions.append(fields)
                    self.action(writer, fields)
                await writer.drain()
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            self.connections.discard(writer)
            self.sessions.discard(task)
            writer.close()
            await writer.wait_closed()

    def broadcast(self, packet):
        for writer in self.connections:
            writer.write(packet)


class MockAMI(MockManager):
    def __init__(self):
        super().__init__()
        self.variables = {}
        self.silent = False

    def action(self, writer, fields):
        if fields["Action"] != "Getvar" or self.silent:
            return
        variable = fields["Variable"]
        value = self.variables.get(variable)
        writer.write(wire(Response="Success" if value is not None else "Error",
                          ActionID=fields["ActionID"], Variable=variable, Value=value or ""))

    def contacts(self, aor, *phones, statuses=None):
        names = [f"{aor};@{index:032x}" for index, _ in enumerate(phones)]
        self.variables[f"PJSIP_AOR({aor},contact)"] = ",".join(names)
        if statuses is None:
            statuses = ["NonQualified"] * len(phones)
        for name, phone, status in zip(names, phones, statuses, strict=True):
            self.variables[f"PJSIP_CONTACT({name},uri)"] = f"sip:{aor}@{LOOPBACK}:{phone.port};transport=udp"
            self.variables[f"PJSIP_CONTACT({name},status)"] = status

    def sms_out(self, endpoint, destination, body="example text", from_header=None):
        def b64(value):
            return base64.b64encode(value.encode()).decode()

        self.broadcast(wire(Event="UserEvent", UserEvent="SmsOut",
                            From64=b64(from_header or f"<sip:{endpoint}@pbx.example.net>"),
                            Destination64=b64(destination), Body64=b64(body)))


class MockTG(MockManager):
    def __init__(self):
        super().__init__()
        self.status = "1"
        self.sent = []
        self.disconnect_after_command = False

    def action(self, writer, fields):
        command = fields["command"]
        prefix, encoded, suffix = command.split('"')
        _, _, _, span, number = prefix.split()
        sms_id = suffix.strip()
        text = unquote_plus(encoded, errors="surrogatepass")
        # The gateway garbles four-byte sequences and accepts surrogate pairs.
        units = "".join(char if ord(char) <= 0xFFFF else "\ufffd" for char in text)
        text = units.encode("utf-16-le", errors="surrogatepass").decode("utf-16-le", errors="replace")
        self.sent.append((int(span), number, text))
        writer.write(b"Response: Follows\r\nPrivilege: SMSCommand\r\n--END COMMAND--\r\n\r\n")
        if self.disconnect_after_command:
            writer.close()
        elif self.status is not None:
            self.send_status(sms_id, self.status)

    def send_status(self, sms_id, status):
        self.broadcast(wire(Event="UpdateSMSSend", ID=sms_id, Status=status, Smsc="+12025550100"))

    def inbound(self, span, sender, body, index=None, total=1, ref=None, utf8=False):
        if index is None:
            index = 0 if total == 1 else 1
        if ref is None:
            ref = "" if total == 1 else "70"
        content = quote_plus("\ufeff" + body if utf8 else body,
                             encoding="utf-8" if utf8 else "iso8859_15")
        packet = wire(Event="ReceivedSMS", ID=ref, GsmSpan=span, Sender=sender,
                      Recvtime="1970-01-01 00:00:00", Index=index, Total=total,
                      Smsc="+12025550100", Content=content)
        self.broadcast(packet[:-2] + b"--END SMS EVENT--\r\n\r\n")


class MockPhone(asyncio.DatagramProtocol):
    def __init__(self):
        self.response = 200
        self.messages = []
        self.wrong_transaction = False
        self.response_delays = {}

    async def start(self):
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(lambda: self, local_addr=(LOOPBACK, 0))
        self.port = transport.get_extra_info("sockname")[1]
        return self

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, packet, addr):
        head, body = packet.split(b"\r\n\r\n", 1)
        request, *headers = head.decode().split("\r\n")
        fields = dict(line.split(": ", 1) for line in headers)
        self.messages.append((request, fields, body))
        if self.response:
            call_id = "different@example.net" if self.wrong_transaction else fields["Call-ID"]
            response = (
                f"SIP/2.0 {self.response} Test\r\nVia: {fields['Via']}\r\n"
                f"Call-ID: {call_id}\r\nCSeq: {fields['CSeq']}\r\nContent-Length: 0\r\n\r\n"
            )
            delay = self.response_delays.get(body, 0)
            if delay:
                asyncio.get_running_loop().call_later(delay, self.transport.sendto, response.encode(), addr)
            else:
                self.transport.sendto(response.encode(), addr)
