import unittest
from minecraft_doudou.m1_adapter import CAPABILITIES, M1ActionDenied, format_command, call_m1


class FakeSocket:
    def __init__(self, payload):
        self.chunks = [payload[i:i+7] for i in range(0, len(payload), 7)]
        self.sent = []
        self.closed = False

    def recv(self, n):
        return self.chunks.pop(0) if self.chunks else b""

    def sendall(self, data):
        self.sent.append(data)

    def settimeout(self, t):
        self.timeout = t

    def close(self):
        self.closed = True


class M1AdapterTests(unittest.TestCase):
    def test_read_only_formats(self):
        self.assertEqual(format_command("agent_status"), "agent status")
        self.assertEqual(format_command("scan", ["bed"]), "scan bed")
        self.assertEqual(format_command("scan", ["32"]), "scan 32")
        self.assertEqual(format_command("describe"), "describe")
        self.assertEqual(format_command("inv"), "inv")
        for x in ("scan", "where", "look", "inv", "face", "craft", "attack", "agent_sleep"):
            self.assertIn(x, CAPABILITIES)

    def test_denies_unverified_and_injected_commands(self):
        for verb, args in [
            ("cmd", ["op anyone"]), ("craft", ["planks"]),
            ("agent_sleep", []), ("attack", ["nearest"]),
            ("goto", ["1", "64", "1"]), ("hold", ["0"]),
            ("scan", ["bed\ncmd op"]), ("scan", ["33"]),
            ("where", ["and extra"]), ("fakeverb", []),
        ]:
            with self.subTest(verb=verb), self.assertRaises(M1ActionDenied):
                format_command(verb, args)

    def test_face_needs_two_confirmations(self):
        for approve, stopped in ((False, False), (True, False), (False, True)):
            with self.assertRaises(M1ActionDenied):
                format_command("face", ["90", "0"], approve_face=approve, baritone_stopped=stopped)
        self.assertEqual(format_command("face", ["-150", "5"],
                         approve_face=True, baritone_stopped=True), "face -150 5")
        for args in (["nan"], ["inf"], ["181"], ["90", "-91"], ["0\ncmd"], []):
            with self.subTest(args=args), self.assertRaises(M1ActionDenied):
                format_command("face", args, approve_face=True, baritone_stopped=True)

    def test_tcp_greeting_framing_and_exact_command(self):
        fake = FakeSocket(b"M1 v0.17\n<<END\nOK RAW\n<<END\nOK x=1\n<<END\n")
        out = call_m1("where", connect=lambda address, timeout: fake)
        self.assertEqual(fake.sent, [b"RAW ON\n", b"where\n"])
        self.assertEqual(out["response"], "OK x=1")
        self.assertFalse(out["verified_in_game"])
        self.assertTrue(fake.closed)

    def test_denied_never_connects(self):
        def fail(*args):
            raise AssertionError("network should never be touched")
        with self.assertRaises(M1ActionDenied):
            call_m1("craft", ["planks"], connect=fail)


if __name__ == "__main__":
    unittest.main()
