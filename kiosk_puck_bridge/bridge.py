"""Coordinate Kiosk Satellite and EchoMuse wake arbitration on the LAN."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import base64
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import ssl
import time

from aioesphomeapi import APIClient, VoiceAssistantEventType as Event
from websockets.asyncio.client import connect
from zeroconf import IPVersion
from zeroconf.asyncio import AsyncZeroconf

LOG = logging.getLogger("kiosk-puck")
KIOSK_PORT = 2330
ECHO_SERVICE = "_esphomelib._tcp.local."
MAX_PEERS = 128
PEER_LIFETIME = 1800
MAX_KIOSK_ENERGY = 120
ECHO_WIN_ENERGY = MAX_KIOSK_ENERGY + 1
CAPABILITIES = ["mic", "speaker", "oww_trigger", "oww_local_only", "wake_word_off"]
IDENTITY = re.compile(r"KIOSK-[0-9a-f]{16}\Z")


def phrase(value):
    return " ".join(value.casefold().split())


def write_private(path, content):
    temp = path.with_suffix(".tmp")
    with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
                   "w", encoding="utf-8") as output:
        output.write(content)
    temp.replace(path)


def device_id(data_dir):
    path = data_dir / "identity"
    if not path.exists():
        write_private(path, f"KIOSK-{secrets.token_hex(8)}\n")
    identity = path.read_text().strip()
    if not IDENTITY.fullmatch(identity):
        raise ValueError("Invalid saved virtual puck identity")
    return identity


def parse_claim(claim):
    try:
        if not isinstance(claim, dict) or claim.get("ks") != "wake" or claim.get("v") != 1:
            return None
        sender, nonce, word, energy = (claim.get(k) for k in ("id", "n", "p", "e"))
        if not isinstance(sender, str) or not 1 <= len(sender) <= 64:
            return None
        if type(nonce) is not int or type(energy) not in (int, float):
            return None
        if not -MAX_KIOSK_ENERGY <= energy <= MAX_KIOSK_ENERGY or not isinstance(word, str):
            return None
        if not 0 < len(word) <= 128 or not phrase(word):
            return None
        return (sender, nonce), float(energy), phrase(word)
    except (ValueError, OverflowError, RecursionError):
        return None


def registration(identity, capabilities):
    return json.dumps({"type": "register", "device_id": identity,
                       "version": "KioskBridge-0.3.1", "board": "unknown",
                       "base_os": "unknown", "capabilities": capabilities})


def controller_url(options, tls):
    host = options["controller_host"]
    port = options["controller_tls_port" if tls else "controller_port"]
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{'wss' if tls else 'ws'}://{host}:{port}"


CREDENTIAL_DIR = "/data/local/etc/echomuse"
TRANSFER = re.compile(
    rf"base64 -d << '__END_B64_42__' > ({CREDENTIAL_DIR}/(?:ca\.pem|token)\.part)"
    r" && chmod (644|600) \1 && echo TRANSFER_OK\n"
)
VERIFY = re.compile(
    rf'GOT=\$\(md5sum ({CREDENTIAL_DIR}/(?:ca\.pem|token)\.part) 2>/dev/null\); '
    r'case "\$GOT" in ([a-f0-9]{32})\*\) mv \1 (\S+) && echo VERIFY_OK ;; '
    r'\*\) rm -f \1; echo "VERIFY_BAD:\$GOT" ;; esac\n'
)
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")


class CredentialReceiver:
    """Handle only EchoMuse's credential transfer, without running shell commands."""

    def __init__(self, path):
        self.path = path
        self.ca = None

    async def receive(self, ws):
        command = await ws.recv()
        if command == f"mkdir -p {CREDENTIAL_DIR} ; echo '__CMD_DONE_9f3a__'\n":
            await ws.send("__CMD_DONE_9f3a__\n")
            return
        if not isinstance(command, str) or not (
            command.startswith("if echo dGVzdA== | busybox base64 -d ")
            and command.endswith("echo __DETECT_DONE__\n")
            and f"[ -d {CREDENTIAL_DIR} ]" in command
        ):
            # EchoMuse also uses the shell plane for maintenance on physical
            # pucks. This virtual device implements only credential transfer.
            return
        await ws.send("DECODER:base64\nMD5:plain\nDESTDIR:ok\n__DETECT_DONE__\n")
        command = await ws.recv()
        transfer = TRANSFER.fullmatch(command) if isinstance(command, str) else None
        if not transfer:
            return
        landing, mode = transfer.groups()
        ca = landing.endswith("/ca.pem.part")
        if mode != ("644" if ca else "600"):
            return
        encoded = ""
        async for line in ws:
            if line == "__END_B64_42__\n":
                break
            if not isinstance(line, str) or len(encoded) + len(line) > 16384:
                return
            encoded += line.strip()
        else:
            return
        try:
            data = base64.b64decode(encoded, validate=True)
            value = data.decode("ascii")
            if ca:
                tls_context({"ca_pem": value})
            elif not self.ca or not TOKEN.fullmatch(value):
                return
        except (ValueError, ssl.SSLError):
            return
        await ws.send("TRANSFER_OK\n")
        command = await ws.recv()
        verify = VERIFY.fullmatch(command) if isinstance(command, str) else None
        if not verify:
            return
        if verify.groups() != (landing, hashlib.md5(data, usedforsecurity=False).hexdigest(),
                               landing.removesuffix(".part")):
            await ws.send("VERIFY_BAD\n")
            return
        if ca:
            self.ca = value
        else:
            write_private(self.path, json.dumps({"ca_pem": self.ca, "token": value}))
        await ws.send("VERIFY_OK\n")
        await ws.wait_closed()


async def request_pairing(control):
    while True:
        await control.send('{"type":"pair_request"}')
        await asyncio.sleep(5)


async def enroll(options, identity, path):
    """Register and wait for EchoMuse's normal device approval and TLS push."""
    if options["controller_host"] not in ("127.0.0.1", "::1"):
        LOG.error("Automatic enrollment requires a stock controller on this HA host")
        return
    origin = controller_url(options, False)
    receiver = CredentialReceiver(path)
    noted = None
    while not path.exists():
        try:
            async with connect(f"{origin}/control", proxy=None, open_timeout=4,
                               close_timeout=1) as control:
                registration_msg = json.loads(registration(identity, ["speaker", "pairing"]))
                registration_msg["pairing"] = True
                await control.send(json.dumps(registration_msg))
                reply = json.loads(await asyncio.wait_for(control.recv(), 5))
                state = reply.get("type")
                if state != noted:
                    LOG.info("EchoMuse enrollment: %s. Approve %s in EchoMuse if prompted.",
                             state, identity)
                    noted = state
                if state == "ack":
                    request = asyncio.create_task(request_pairing(control))
                    try:
                        async for raw in control:
                            message = json.loads(raw)
                            if message.get("type") == "ping":
                                await control.send(json.dumps({
                                    "type": "pong", "id": message.get("id"),
                                    "mono": time.monotonic() * 1000,
                                }))
                            elif message.get("type") == "shell_open" and not message.get("pty"):
                                async with connect(f"{origin}/shell/{identity}", proxy=None,
                                                   open_timeout=4, close_timeout=1,
                                                   max_size=16384) as shell:
                                    try:
                                        async with asyncio.timeout(10):
                                            await receiver.receive(shell)
                                    except TimeoutError:
                                        pass
                                if path.exists():
                                    # The controller commits the token after VERIFY_OK.
                                    await control.wait_closed()
                                    break
                    finally:
                        request.cancel()
                        await asyncio.gather(request, return_exceptions=True)
        except Exception as error:
            LOG.warning("EchoMuse enrollment retry (%s)", type(error).__name__)
        if not path.exists():
            await asyncio.sleep(5)


def tls_context(credentials):
    # Trust the CA explicitly provisioned by the operator for this controller.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cadata=credentials["ca_pem"])
    return context


@dataclass
class Claim:
    session: int
    started: float
    word: str
    level: float
    submitted: bool = False
    resolved: bool = False


class Bridge(asyncio.DatagramProtocol):
    def __init__(self, options, identity, credentials):
        self.options = options
        self.identity = identity
        self.credentials = credentials
        self.mode = options["mode"]
        self.peers = {}
        self.seen = {}
        self.claim = None
        self.session = secrets.randbits(31)
        self.nonce = secrets.randbits(30)
        self.control = None
        self.data = None
        self.voice = None
        self.transport = None
        self.wake_enabled = True
        self.tasks = set()

    def connection_made(self, transport):
        self.transport = transport

    def send_packet(self, packet, address):
        self.transport.sendto(json.dumps(packet, separators=(",", ":")).encode(), address)

    def hello(self, address):
        self.send_packet({"ks": "wake", "v": 1, "id": self.identity, "hello": True}, address)

    async def advertise(self):
        address = self.options.get("discovery_address", "255.255.255.255")
        while True:
            if address:
                self.hello((address, KIOSK_PORT))
            await asyncio.sleep(300)

    def task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.task_finished)
        return task

    def task_finished(self, task):
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            LOG.error("Bridge task failed (%s)", type(task.exception()).__name__)

    def datagram_received(self, data, address):
        if address[1] != KIOSK_PORT or len(data) > 1024:
            return
        try:
            packet = json.loads(data)
        except (ValueError, RecursionError):
            return
        if not isinstance(packet, dict) or packet.get("id") == self.identity:
            return
        result = parse_claim(packet)
        sender = packet.get("id")
        is_hello = (packet.get("ks") == "wake" and packet.get("v") == 1
                    and packet.get("hello") is True
                    and isinstance(sender, str) and 1 <= len(sender) <= 64)
        if not result and not is_hello:
            return
        now = time.monotonic()
        self.peers = {peer: ts for peer, ts in self.peers.items() if now - ts < PEER_LIFETIME}
        if address not in self.peers:
            if len(self.peers) >= MAX_PEERS:
                return
            LOG.info("Discovered Kiosk wake peer %s", address[0])
            self.hello(address)
        self.peers[address] = now
        if not result:
            return
        key, energy, word = result
        self.seen = {key: ts for key, ts in self.seen.items() if now - ts < 3}
        if key in self.seen or len(self.seen) >= 256:
            return
        self.seen[key] = now
        level = min(0.0, max(-120.0, energy + self.options["level_offset_db"]))
        LOG.info("Kiosk %s heard %s at %.1f dB above noise", address[0], word, energy)
        if self.mode != "bridge" or not self.ready:
            return
        current = self.claim
        if current and now - current.started < .75:
            if not current.submitted and current.word == word:
                current.level = max(current.level, level)
            return
        self.session = self.session % 0xFFFFFFFF + 1
        self.claim = Claim(self.session, now, word, level)
        self.task(self.submit_wake(self.claim))

    @property
    def ready(self):
        return bool(self.control and self.data and self.voice and self.wake_enabled)

    async def send_control(self, message):
        if self.control is not None:
            await self.control.send(json.dumps(message))

    async def submit_wake(self, claim):
        # Coalesce direct and broadcast copies, and nearby Kiosk claims
        # that arrive in the same small interval. EchoMuse's hold is 250 ms.
        await asyncio.sleep(.020)
        if self.claim is not claim or not self.ready:
            return
        claim.submitted = True
        await self.send_control({
            "type": "oww_wake", "session": claim.session, "score": 1.0,
            "threshold": 0.5, "ageMs": (time.monotonic() - claim.started) * 1000,
            "capturedMono": claim.started * 1000, "level": claim.level, "peak": claim.level,
        })

    async def relay_echo_win(self, claim):
        # This reserved strength represents EchoMuse's decision for this wake.
        self.nonce = (self.nonce + 1) % (1 << 30)
        packet = {"ks": "wake", "v": 1, "id": self.identity, "p": claim.word,
                  "e": ECHO_WIN_ENERGY, "n": self.nonce}
        LOG.info("EchoMuse chose a real puck for %s; relaying its win", claim.word)
        for delay in (0, .015, .015):
            await asyncio.sleep(delay)
            for peer, last_seen in tuple(self.peers.items()):
                if claim.started - last_seen <= PEER_LIFETIME:
                    self.send_packet(packet, peer)

    async def handle_control(self, ws):
        async for raw in ws:
            msg = json.loads(raw)
            kind = msg.get("type")
            if kind == "ping" and "id" in msg:
                await self.send_control({"type": "pong", "id": msg["id"],
                                         "mono": time.monotonic() * 1000})
            elif kind == "config" and "wakeWordEnabled" in msg:
                self.wake_enabled = bool(msg["wakeWordEnabled"])
            claim = self.claim
            if (not claim or not claim.submitted or claim.resolved
                    or msg.get("session") != claim.session):
                continue
            if kind == "listen_ack":
                claim.resolved = True
                LOG.info("EchoMuse yielded to Kiosk for %s", claim.word)
            elif kind == "listen_close":
                claim.resolved = True
                reason = msg.get("reason")
                if reason == "ceded" and time.monotonic() - claim.started <= .75:
                    self.task(self.relay_echo_win(claim))
                elif reason not in ("turn_end", "end_of_speech"):
                    LOG.info("EchoMuse declined virtual claim (%s); Kiosks decide normally", reason)

    async def run_echo(self):
        url = controller_url(self.options, True)
        context = tls_context(self.credentials)
        settings = {"ssl": context, "server_hostname": "echomuse-controller",
                    "additional_headers": {"X-EM-Token": self.credentials["token"]},
                    "proxy": None, "open_timeout": 5, "close_timeout": 1,
                    "ping_interval": None}
        while True:
            try:
                async with connect(f"{url}/control", **settings) as control:
                    await control.send(registration(self.identity, CAPABILITIES))
                    hello = json.loads(await asyncio.wait_for(control.recv(), 10))
                    if hello.get("type") != "ack":
                        raise RuntimeError(f"EchoMuse registration: {hello.get('type', 'unknown')}")
                    if "listen_session" not in hello.get("features", []):
                        raise RuntimeError("EchoMuse does not support private listening sessions")
                    async with connect(f"{url}/data", **settings) as data:
                        await data.send(json.dumps({"type": "identify", "device_id": self.identity}))
                        self.control = control
                        self.data = data
                        await self.send_control({"type": "listen_state", "state": "local"})
                        LOG.info("Registered %s with stock EchoMuse over TLS", self.identity)
                        tasks = [asyncio.create_task(self.handle_control(control)),
                                 asyncio.create_task(self.drain_data(data))]
                        try:
                            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                            for task in done:
                                task.result()
                        finally:
                            for task in tasks:
                                task.cancel()
                            await asyncio.gather(*tasks, return_exceptions=True)
            except (OSError, ValueError, RuntimeError, TimeoutError) as error:
                LOG.warning("EchoMuse unavailable: %s", error)
            except Exception as error:
                LOG.warning("EchoMuse link interrupted (%s)", type(error).__name__)
            finally:
                self.control = self.data = None
                self.claim = None
            await asyncio.sleep(5)

    async def drain_data(self, data):
        async for frame in data:
            if frame == b"\x03":
                await self.send_control({"type": "playback_stats", "periods": 0,
                                         "underruns": 0})

    async def find_echo_port(self, discovery):
        name = f"echomuse-{self.identity[-12:].lower()}"
        if port := self.options.get("virtual_puck_port"):
            return name, port
        info = await discovery.async_get_service_info(ECHO_SERVICE, f"{name}.{ECHO_SERVICE}", 3000)
        if info is None or not info.port:
            return None
        return name, info.port

    async def run_voice(self):
        # Stock EchoMuse counts only devices with a connected ESPHome client.
        # Connect to our own virtual device, never to the Kiosks. If this
        # device wins, end its empty pipeline locally so the real Kiosk can
        # run the normal Assist pipeline on its existing connection.
        discovery = AsyncZeroconf(ip_version=IPVersion.V4Only)
        try:
            while True:
                if self.control and (endpoint := await self.find_echo_port(discovery)):
                    await self.connect_voice(*endpoint)
                await asyncio.sleep(3)
        finally:
            await discovery.async_close()

    async def connect_voice(self, name, port):
        dead = asyncio.Event()
        client = APIClient(self.options["controller_host"], port, None,
                           expected_name=name, client_info="Kiosk Puck Bridge")

        async def stopped(_expected):
            dead.set()

        async def start(_conversation, _flags, _settings, _word):
            self.task(self.finish_virtual_pipeline(client))
            return 0

        async def stop(_abort):
            pass

        async def audio(_pcm):
            pass

        try:
            await client.connect(on_stop=stopped, login=True)
            info = await client.device_info()
            if info.name != name:
                raise ValueError("Virtual ESPHome device identity does not match")
            client.subscribe_voice_assistant(handle_start=start, handle_stop=stop, handle_audio=audio)
            self.voice = client
            LOG.info("Virtual puck is ready to arbitrate")
            await dead.wait()
        except Exception as error:
            LOG.info("Waiting for the virtual puck's ESPHome connection (%s)", type(error).__name__)
        finally:
            self.voice = None
            await client.disconnect()

    async def finish_virtual_pipeline(self, client):
        await asyncio.sleep(0)
        if self.voice is client:
            client.send_voice_assistant_event(Event.VOICE_ASSISTANT_RUN_END, {})


async def connect_echo(bridge, credentials_path):
    if not credentials_path.exists():
        await enroll(bridge.options, bridge.identity, credentials_path)
    if credentials_path.exists():
        bridge.credentials = json.loads(credentials_path.read_text())
        await bridge.run_echo()


async def main(data_dir):
    options = json.loads((data_dir / "options.json").read_text())
    identity = device_id(data_dir)
    LOG.info("Virtual puck ID: %s", identity)
    credentials_path = data_dir / "credentials.json"
    loop = asyncio.get_running_loop()
    bridge = Bridge(options, identity, None)
    transport, _ = await loop.create_datagram_endpoint(
        lambda: bridge, local_addr=("0.0.0.0", KIOSK_PORT), family=socket.AF_INET,
        allow_broadcast=True)
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        bridge.task(bridge.advertise())
        bridge.task(connect_echo(bridge, credentials_path))
        if bridge.mode == "bridge":
            bridge.task(bridge.run_voice())
        LOG.info("Listening for Kiosk wake claims on UDP 2330 in %s mode", bridge.mode)
        await stop.wait()
    finally:
        transport.close()
        tasks = list(bridge.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(main(Path(os.environ.get("BRIDGE_DATA", "/data"))))
    except (OSError, ValueError, RuntimeError) as error:
        LOG.error("%s", error)
        raise SystemExit(1) from None
    except Exception as error:
        # HTTP errors can carry credentials in their URLs or request headers.
        LOG.error("Bridge startup failed (%s, HTTP status %s)",
                  type(error).__name__, getattr(error, "status", "unknown"))
        raise SystemExit(1) from None
