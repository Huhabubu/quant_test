"""No network or game required: deterministic tests for M1 socket framing."""
import socket
import unittest

from minecraft_doudou.m1_probe import (
    M1ProbeError, M1Reader, probe, HOST, MAX_REPLY_BYTES,
)


class FakeSocket:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.sent = []
        self.timeout = None
        self.closed = False

    def settimeout(self, timeout):
        self.timeout = timeout

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, size):
        if not self.chunks:
            return b""
        return self.chunks.pop(0)

    def close(self):
        self.closed = True


class ProbeTests(unittest.TestCase):
    def test_fragmented_sentinel_and_tail_retention(self):
        client = FakeSocket([b"greet\r\n<<EN", b"D\r\nresponse one\n<<END\nnext\n<<END\n"])
        r = M1Reader(client)
        self.assertEqual(r.read_reply(), "greet")
        self.assertEqual(r.read_reply(), "response one")
        self.assertEqual(r.read_reply(), "next")
        self.assertEqual(client.chunks, [])

    def test_outbound_command_whitelist(self):
        s = FakeSocket([b"where result\n<<END\n"])
        r = M1Reader(s)
        for bad in ("face 90", "attack nearest", "defend on", "sendchat hi",
                    "connect server", "quit", "RAW ON\nattack", "Where", ""):
            with self.subTest(bad=bad), self.assertRaises(M1ProbeError):
                r.command(bad)
        self.assertEqual(s.sent, [])
        self.assertEqual(r.command("where"), "where result")
        self.assertEqual(s.sent, [b"where\n"])

    def test_probe_only_local_readonly_commands(self):
        replies = [
            "Connected to Machine One (M1).\n<<END\n",
            "OK RAW ON\n<<END\n",
            "help lines\n<<END\n",
            "where result\n<<END\n",
            "look: nothing in reach\n<<END\n",
            "inventory contents\n<<END\n",
        ]
        data = "".join(replies).encode()
        s = FakeSocket([data[:4], data[4:75], data[75:]])
        addresses = []

        def connect(address, timeout):
            addresses.append((address, timeout))
            return s

        result = probe(connect=connect)
        self.assertTrue(result["connected"])
        self.assertTrue(result["read_only"])
        self.assertEqual(addresses, [((HOST, 26000), 6)])
        self.assertEqual(s.sent, [b"RAW ON\n", b"help\n", b"where\n",
                                  b"look\n", b"inv\n"])
        self.assertTrue(s.closed)
        self.assertEqual(s.timeout, 15)
        self.assertIn("where", result["results"])
        self.assertIn("nothing in reach", result["results"]["look"]["response"])

    def test_refuse_unknown_service_on_local_port(self):
        s = FakeSocket([b"Some other server\n<<END\n"])
        with self.assertRaisesRegex(M1ProbeError, "unexpected_server_banner"):
            probe(connect=lambda address, timeout: s)
        self.assertEqual(s.sent, [])
        self.assertTrue(s.closed)

    def test_incomplete_sentinel_fails_closed(self):
        s = FakeSocket([b"hello\n<<EN"])
        with self.assertRaisesRegex(M1ProbeError, "without_sentinel"):
            M1Reader(s).read_reply()

    def test_reply_size_bound(self):
        s = FakeSocket([b"x" * (MAX_REPLY_BYTES + 1)])
        with self.assertRaisesRegex(M1ProbeError, "reply_exceeds_limit"):
            M1Reader(s).read_reply()

    def test_invalid_port(self):
        for value in (-1, 0, 65536, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                probe(connect=lambda address, timeout: None, port=value)


if __name__ == "__main__":
    unittest.main()
