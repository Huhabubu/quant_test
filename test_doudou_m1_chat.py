"""No real M1 or Minecraft process is contacted by these tests."""
import unittest

from minecraft_doudou.m1_chat import (
    M1ChatError, format_m1_say, send_m1_chat,
)


class FakeSocket:
    def __init__(self, wire):
        self.parts = [wire[i:i + 3] for i in range(0, len(wire), 3)]
        self.sent = []
        self.closed = False

    def recv(self, count):
        return self.parts.pop(0) if self.parts else b""

    def sendall(self, data):
        self.sent.append(data)

    def settimeout(self, timeout):
        self.timeout = timeout

    def close(self):
        self.closed = True


class M1ChatTests(unittest.TestCase):
    def test_one_exact_unicode_chat_command(self):
        self.assertEqual(format_m1_say("你好，豆豆！"), "say 你好，豆豆！")
        fake = FakeSocket(
            b"Connected to Machine One (M1)\n<<END\n"
            b"OK RAW\n<<END\n"
            b"[auto-upgrade] CHEST: nothing -> iron_chestplate\n"
            b"OK chat submitted\n<<END\n"
        )
        result = send_m1_chat("你好，豆豆！",
                              connect=lambda addr, timeout: fake)
        self.assertEqual(fake.sent, [b"RAW ON\n",
                                     "say 你好，豆豆！\n".encode("utf-8")])
        self.assertEqual(result, {
            "accepted": True, "verified": False, "transport": "m1",
        })
        self.assertGreater(fake.timeout, 0)
        self.assertLessEqual(fake.timeout, 15)
        self.assertTrue(fake.closed)

    def test_rejects_commands_control_chars_and_oversized_chat(self):
        values = [
            "", " ", "\t", "hi\nattack nearest",
            "hello\r\nstop", "x\x00y", "hi\u2028bye",
            "hi\u2029bye", "hi\u200bbye",
            "/tp 1 2 3", "  /op user", "#follow player",
            "  #goto 5 70 5", "甲" * 201, 1,
        ]
        for text in values:
            with self.subTest(text=text), self.assertRaises(M1ChatError):
                format_m1_say(text)
        self.assertEqual(format_m1_say("这不是 /give 指令"), "say 这不是 /give 指令")

    def test_fails_before_sending_on_bad_greeting(self):
        fake = FakeSocket(b"bad service\n<<END\n")
        with self.assertRaisesRegex(M1ChatError, "banner"):
            send_m1_chat("你好", connect=lambda *_: fake)
        self.assertEqual(fake.sent, [])
        self.assertTrue(fake.closed)

    def test_rejects_raw_mode_failure_without_say(self):
        fake = FakeSocket(b"M1\n<<END\nERR raw disabled\n<<END\n")
        with self.assertRaisesRegex(M1ChatError, "raw_mode"):
            send_m1_chat("你好", connect=lambda *_: fake)
        self.assertEqual(fake.sent, [b"RAW ON\n"])
        self.assertTrue(fake.closed)

    def test_say_error_is_not_success(self):
        for response in (b"ERR not in world\n",
                         b"FAIL chat blocked\n",
                         b"command accepted?\n",
                         b"[auto-upgrade] OK armor\nERR chat fail\n"):
            fake = FakeSocket(b"M1\n<<END\nOK RAW\n<<END\n" + response + b"<<END\n")
            with self.subTest(response=response), self.assertRaisesRegex(M1ChatError, "m1_say"):
                send_m1_chat("你好", connect=lambda *_: fake)
            self.assertEqual(fake.sent, [b"RAW ON\n", "say 你好\n".encode("utf-8")])
            self.assertTrue(fake.closed)

    def test_async_chat_before_raw_and_say_receipts_is_skipped(self):
        fake = FakeSocket(
            b"M1 v0.17\n<<END\n"
            b"[chat] <Player> old chat from the game\n<<END\n"
            b"[event] inventory changed\n<<END\n"
            b"OK RAW\n<<END\n"
            b"[chat] <Owner> OK chat submitted\n"
            b"OK\n<<END\n"
            b"OK chat submitted\n<<END\n"
        )
        result = send_m1_chat("游戏测试", connect=lambda *_: fake)
        self.assertEqual(result["skipped_async_frames"], 3)
        self.assertEqual(result["transport"], "m1")
        self.assertFalse(result["verified"])
        self.assertEqual(fake.sent, [b"RAW ON\n", "say 游戏测试\n".encode("utf-8")])

    def test_async_before_banner_is_not_misread_as_banner(self):
        fake = FakeSocket(
            b"[chat] <Player> said M1 is ready\n<<END\n"
            b"Machine One ready\n<<END\n"
            b"OK RAW\n<<END\n"
            b"OK chat submitted\n<<END\n"
        )
        result = send_m1_chat("你好", connect=lambda *_: fake)
        self.assertEqual(result["skipped_async_frames"], 1)
        self.assertEqual(fake.sent, [b"RAW ON\n", "say 你好\n".encode("utf-8")])

    def test_many_async_frames_fail_closed_before_say(self):
        from minecraft_doudou.m1_chat import MAX_ASYNC_FRAMES
        fake = FakeSocket(
            b"M1\n<<END\n" +
            b"[chat] <Player> noise\n<<END\n" * (MAX_ASYNC_FRAMES + 1)
        )
        with self.assertRaisesRegex(M1ChatError, "raw_mode_too_many_async_frames"):
            send_m1_chat("不要发送", connect=lambda *_: fake)
        self.assertEqual(fake.sent, [b"RAW ON\n"])

    def test_async_followed_by_error_does_not_count_as_ok(self):
        fake = FakeSocket(
            b"M1\n<<END\n[chat] <Someone> hi\n<<END\n"
            b"ERR RAW rejected\n<<END\n"
        )
        with self.assertRaisesRegex(M1ChatError, "raw_mode_not_confirmed"):
            send_m1_chat("你好", connect=lambda *_: fake)
        self.assertEqual(fake.sent, [b"RAW ON\n"])

    def test_unknown_frame_after_async_fails_closed(self):
        fake = FakeSocket(
            b"M1\n<<END\n"
            b"[chat] <Someone> queued\n<<END\n"
            b"WHAT IS THIS\n<<END\n"
        )
        with self.assertRaisesRegex(M1ChatError, "raw_mode_not_confirmed"):
            send_m1_chat("你好", connect=lambda *_: fake)
        self.assertEqual(fake.sent, [b"RAW ON\n"])

    def test_chat_containing_ok_never_confirms_raw_mode(self):
        fake = FakeSocket(
            b"M1\n<<END\n"
            b"[chat] <Someone> literal OK\nOK RAW\n<<END\n"
        )
        with self.assertRaises(Exception):
            send_m1_chat("不能发", connect=lambda *_: fake)
        self.assertEqual(fake.sent, [b"RAW ON\n"])

    def test_async_can_arrive_after_raw_and_say_failure_is_unverified(self):
        fake = FakeSocket(
            b"M1\n<<END\n"
            b"OK RAW\n<<END\n"
            b"[event] player_nearby\n<<END\n"
            b"ERR server rejected message\n<<END\n"
        )
        with self.assertRaisesRegex(M1ChatError, "m1_say_not_confirmed"):
            send_m1_chat("不能算成功", connect=lambda *_: fake)
        self.assertEqual(fake.sent, [b"RAW ON\n",
                                     "say 不能算成功\n".encode("utf-8")])

    def test_socket_reply_missing_sentinel_is_uncertain(self):
        fake = FakeSocket(b"M1\n<<END\nOK RAW\n<<END\nOK partial")
        with self.assertRaises(Exception):
            send_m1_chat("测试", connect=lambda *_: fake)
        self.assertEqual(fake.sent, [b"RAW ON\n", "say 测试\n".encode("utf-8")])
        self.assertTrue(fake.closed)


if __name__ == "__main__":
    unittest.main()
