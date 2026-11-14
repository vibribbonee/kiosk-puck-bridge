"""Exercise virtual puck identity, discovery and both arbitration outcomes."""
import asyncio
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "kiosk_puck_bridge"))
import bridge


class Wire:
    def __init__(self, pending=()):
        self.sent = []
        self.pending = pending

    async def send(self, value):
        self.sent.append(json.loads(value))

    async def __aiter__(self):
        for value in self.pending:
            yield json.dumps(value)


class DatagramSink:
    def __init__(self):
        self.sent = []

    def sendto(self, message, address):
        self.sent.append((json.loads(message), address))


KIOSK_A = ("192.0.2.15", bridge.KIOSK_PORT)
KIOSK_B = ("192.0.2.16", bridge.KIOSK_PORT)


def wake(sender, energy, nonce=42):
    return json.dumps({"ks": "wake", "v": 1, "p": "Hey Assistant", "id": sender,
                       "e": energy, "n": nonce}).encode()


def make_bridge(mode="bridge"):
    app = bridge.Bridge({"mode": mode, "level_offset_db": -55}, "KIOSK-test", None)
    app.connection_made(DatagramSink())
    app.control = Wire()
    app.data = Wire()
    app.voice = object()
    return app


def test_stable_identity(tmp_path):
    assert bridge.device_id(tmp_path) == bridge.device_id(tmp_path)
    assert (tmp_path / "identity").stat().st_mode & 0o777 == 0o600


def test_auto_discovery_in_observe_mode():
    app = make_bridge("observe")
    claim = wake("kiosk-a", 30)
    assert bridge.parse_claim(json.loads(claim))[2] == "hey assistant"
    app.datagram_received(claim, (KIOSK_A[0], 1234))
    app.datagram_received(wake(app.identity, 30), KIOSK_B)
    app.datagram_received(wake("bad", float("nan")), KIOSK_B)
    assert not app.peers

    app.datagram_received(claim, KIOSK_A)
    app.datagram_received(wake("kiosk-b", 34), KIOSK_B)
    assert set(app.peers) == {KIOSK_A, KIOSK_B}
    assert set(app.seen) == {("kiosk-a", 42), ("kiosk-b", 42)}
    assert app.claim is None
    assert not app.control.sent


@pytest.mark.parametrize("result", ["listen_ack", "ceded", "no_ha"])
def test_one_vote_and_relay_only_when_echo_wins(result):
    async def run():
        app = make_bridge()
        claim = wake("kiosk-a", 30)
        app.datagram_received(claim, KIOSK_A)
        app.datagram_received(claim, KIOSK_A)
        app.datagram_received(wake("kiosk-b", 34), KIOSK_B)
        await asyncio.gather(*app.tasks)
        assert len(app.control.sent) == 1
        assert app.control.sent[0]["level"] == -21
        assert app.control.sent[0]["type"] == "oww_wake"

        response = {"type": "listen_close", "session": app.claim.session, "reason": result}
        if result == "listen_ack":
            response["type"] = result
        stale = {**response, "type": "listen_close", "reason": "ceded", "session": 0}
        await app.handle_control(Wire([stale, response, response]))
        await asyncio.gather(*app.tasks)
        relays = [(packet, addr) for packet, addr in app.transport.sent if "e" in packet]
        if result == "ceded":
            assert [addr for _, addr in relays].count(KIOSK_A) == 3
            assert [addr for _, addr in relays].count(KIOSK_B) == 3
            assert {p["e"] for p, _ in relays} == {bridge.ECHO_WIN_ENERGY}
            assert {p["p"] for p, _ in relays} == {"hey assistant"}
            assert len({p["n"] for p, _ in relays}) == 1
        else:
            assert not relays
        assert not app.data.sent

    asyncio.run(run())
