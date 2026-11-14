"""Run against an extracted copy of the upstream (unmodified) controller."""
import asyncio
from contextlib import suppress
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory

from aiohttp import ClientSession
import bcrypt

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "kiosk_puck_bridge"))
sys.path.insert(0, str(Path(sys.argv[1]).resolve() / "controller"))

from scenarios import harness
from scenarios.harness import Controller, FakeEcho, FakeHA, TtsServer, tone, wait_for, wav_bytes
import bridge


def local_port(below=65536):
    # macOS can allocate every ephemeral port above EchoMuse's allowed base.
    if below < 60000:
        for number in range(41200, 42100, 4):
            with socket.socket() as first, socket.socket() as second:
                try:
                    first.bind(("127.0.0.1", number))
                    second.bind(("127.0.0.1", number + 1))
                    return number
                except OSError:
                    continue
    with socket.socket() as endpoint:
        endpoint.bind(("127.0.0.1", 0))
        return endpoint.getsockname()[1]


harness.free_port = local_port


class DatagramSink:
    def __init__(self):
        self.sent = []

    def sendto(self, message, address):
        self.sent.append((json.loads(message), address))

    @property
    def wins(self):
        return [(p, addr) for p, addr in self.sent if p.get("e") == bridge.ECHO_WIN_ENERGY]


class ObservedBridge(bridge.Bridge):
    def __init__(self, options, identity, credentials):
        super().__init__(options, identity, credentials)
        self.virtual_runs = 0

    async def finish_virtual_pipeline(self, client):
        self.virtual_runs += 1
        await super().finish_virtual_pipeline(client)


KIOSKS = (("192.0.2.15", bridge.KIOSK_PORT), ("192.0.2.16", bridge.KIOSK_PORT))


def contend(virtual, nonce, energy):
    for index, address in enumerate(KIOSKS):
        virtual.datagram_received(json.dumps({
            "ks": "wake", "v": 1, "p": "hey assistant", "id": f"kiosk-{index}",
            "e": energy - index, "n": nonce,
        }).encode(), address)


async def run(temp):
    controller = Controller(temp, ECHOMUSE_HOME_ASSISTANT_INGRESS="false")
    virtual_tasks = []
    real = real_ha = tts = speech = None
    try:
        await controller.start()
        controller.execute("UPDATE system_config SET value = 'strict' WHERE key='device_approval'")
        api = f"http://127.0.0.1:{controller.api_port}"
        password = "local-test-password"
        phash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        controller.execute("INSERT INTO users (username,password_hash,role,created_at) "
                           "VALUES (?,?,?,?)", "tester", phash, "admin", 0)
        async with ClientSession() as client:
            async with client.post(f"{api}/api/auth/login",
                                   json={"username": "tester", "password": password}) as response:
                response.raise_for_status()
                token = (await response.json())["token"]
        options = {
            "controller_host": "127.0.0.1", "controller_tls_port": int(controller.env["SERVER_TLS_PORT"]),
            "controller_port": controller.port,
            "mode": "bridge",
            "level_offset_db": -55,
        }
        identity = bridge.device_id(temp)
        credentials_path = temp / "credentials.json"
        enrollment = asyncio.create_task(bridge.enroll(options, identity, credentials_path))
        try:
            await wait_for(lambda: controller.query(
                "SELECT approved FROM devices WHERE device_id=?", identity), 6, "pending puck")
            assert not controller.query(
                "SELECT approved FROM devices WHERE device_id=?", identity)[0]["approved"]
            async with ClientSession(headers={"Authorization": f"Bearer {token}"}) as client:
                async with client.post(f"{api}/api/devices/{identity}/approve",
                                       json={"label": "Bridge"}) as response:
                    response.raise_for_status()
            await asyncio.wait_for(enrollment, 30)
        finally:
            enrollment.cancel()
            await asyncio.gather(enrollment, return_exceptions=True)
        credentials = json.loads(credentials_path.read_text())
        assert credentials_path.stat().st_mode & 0o777 == 0o600
        assert controller.query("SELECT approved, label FROM devices WHERE device_id=?", identity)[0]["approved"]
        virtual = ObservedBridge(options, identity, credentials)
        sink = DatagramSink()
        virtual.connection_made(sink)
        virtual_tasks.append(asyncio.create_task(virtual.run_echo()))
        await wait_for(lambda: virtual.control and virtual.data, 8, "virtual puck TLS")
        port = await wait_for(lambda: controller.esphome_port(identity), 8, "virtual ESPHome port")
        await wait_for(lambda: f"Listening on 127.0.0.1:{port}" in controller.log(),
                       8, "virtual ESPHome listener")
        await wait_for(lambda: f"[{identity}] listening privately" in controller.log(),
                       8, "virtual private listening")
        virtual_tasks.append(asyncio.create_task(virtual.run_voice()))
        await wait_for(lambda: virtual.ready, 5, "virtual puck ready")
        await asyncio.sleep(.1)

        controller.execute("UPDATE system_config SET value = 'auto' WHERE key='device_approval'")
        real = FakeEcho(controller)
        await real.connect()
        real_port = await wait_for(lambda: controller.esphome_port(real.device_id), 5, "real Echo port")
        await wait_for(lambda: f"Listening on 127.0.0.1:{real_port}" in controller.log(),
                       8, "real ESPHome listener")
        real_ha = FakeHA(real_port)
        await real_ha.connect()
        tts = TtsServer()
        await tts.start()
        reply = tts.add("reply.wav", wav_bytes(tone(.24, 16000), 16000))
        real_ha.pipeline = lambda h: h.answer("what time is it", "test answer", reply)
        await asyncio.sleep(.5)
        pcm = tone(.8, 16000)

        contend(virtual, 1, 48)
        speech = asyncio.create_task(real.wake(pcm, level=-45, peak=-44))
        await wait_for(lambda: virtual.virtual_runs == 1, 5, "virtual ESPHome request")
        await speech
        await wait_for(lambda: any(
            m.get("reason") == "ceded" for m in real.sent("listen_close")
        ), 4, "real Echo yielded")
        assert not real_ha.starts
        assert not sink.wins
        print("Stock EchoMuse: automatic TLS after approval; louder Kiosk beat the real Echo.")
        end_of_first_contest = virtual.claim.started + .8
        await asyncio.sleep(max(0, end_of_first_contest - bridge.time.monotonic()))

        contend(virtual, 2, 8)
        speech = asyncio.create_task(real.wake(pcm, level=-20, peak=-19))
        await wait_for(lambda: len(sink.wins) >= 2, 3, "real Echo win relayed to Kiosks")
        assert {addr for _, addr in sink.wins} == set(KIOSKS)
        assert {p["p"] for p, _ in sink.wins} == {"hey assistant"}
        await speech
        await wait_for(lambda: real.voice_eos, 6, "real Echo got the reply")
        assert virtual.virtual_runs == 1 and len(real_ha.starts) == 1
        print("Stock EchoMuse: louder real Echo won and played the reply; both Kiosks got its verdict.")
    except Exception:
        lines = controller.log().splitlines()
        safe = [line for line in lines if not line.lstrip().startswith("│")]
        print("\n".join(safe)[-5500:])
        raise
    finally:
        if speech:
            speech.cancel()
            with suppress(asyncio.CancelledError):
                await speech
        for task in virtual_tasks:
            task.cancel()
        await asyncio.gather(*virtual_tasks, return_exceptions=True)
        if real_ha:
            await real_ha.disconnect()
        if real:
            await real.close()
        if tts:
            await tts.stop()
        controller.stop()


with TemporaryDirectory(prefix="kiosk-bridge-stock-") as directory:
    asyncio.run(run(Path(directory)))
