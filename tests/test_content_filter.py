import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import time

from content_filter import (
    get_skip_reason,
    split_for_discord,
    looks_like_code,
    protect_segments,
    restore_segments,
)

CODE_SAMPLES = {
    "html": '<div class="card">\n  <p>Hello</p>\n</div>',
    "html_doc": "<!DOCTYPE html>\n<html><body>Hi</body></html>",
    "html_one_line": '<script src="app.js"></script>',
    "css": ".button {\n  color: red;\n  padding: 4px;\n}",
    "css_inline": "body { margin: 0; }\nh1 { font-size: 2rem; }",
    "js": "const x = 5;\nif (x > 3) {\n  console.log(x);\n}",
    "js_arrow": "button.addEventListener('click', () => {\n  run();\n});",
    "js_function": "function add(a, b) { return a + b; }",
    "php": "<?php\n$name = 'Tom';\necho $name;",
    "php_no_tag": "$user = getUser();\n$user->save();\nreturn $user;",
    "python": "def hello(name):\n    print(name)",
    "c": "#include <stdio.h>\nint main() { return 0; }",
    "json_like": '{\n  "name": "bot",\n  "version": 2\n}',
}

PROSE_SAMPLES = [
    "Hello everyone, how are you today?",
    "if you can (please) help me with this",
    "for real (lol) that was crazy",
    "привет\n))",
    "see you tomorrow ;)",
    "ok;",
    "Note: bring snacks and water",
    "I love <3 this server",
    "The function of this channel is to share memes",
    "Rules:\n1. Be nice\n2. No spam\n3. Have fun!",
    "Labas! Kaip sekasi? Ar žaisim šiandien?",
    "今日は何をしますか",
    "x = 5 is the answer to question 2",
]


class LooksLikeCodeTests(unittest.TestCase):
    def test_detects_unfenced_code(self):
        for name, sample in CODE_SAMPLES.items():
            with self.subTest(sample=name):
                self.assertEqual(get_skip_reason(sample), "code")

    def test_regular_chat_is_not_code(self):
        for sample in PROSE_SAMPLES:
            with self.subTest(sample=sample):
                self.assertFalse(looks_like_code(sample))
                self.assertIsNone(get_skip_reason(sample))


class SkipReasonTests(unittest.TestCase):
    def test_nothing_to_translate(self):
        for sample in [
            "", "   ", "😀😀", "k", "?", "123", "!!!", ":)",
            "https://example.com/page",
            "<@123456789012345678>",
            "<@123456789012345678> <#123456789012345678> 👍",
            "<:pepe:123456789012345678>",
            "```py\nprint('hi')\n```",
            "`npm install`",
            "<t:1700000000:R>",
        ]:
            with self.subTest(sample=sample):
                self.assertEqual(get_skip_reason(sample), "no_text")

    def test_short_words_are_still_translated(self):
        for sample in ["ok", "gg", "да", "好", "Hi!"]:
            with self.subTest(sample=sample):
                self.assertIsNone(get_skip_reason(sample))

    def test_bot_commands(self):
        for sample in ["!play", "?rank", ".help", "$balance", "-skip", "!daily-reward"]:
            with self.subTest(sample=sample):
                self.assertEqual(get_skip_reason(sample), "command")

    def test_messages_that_only_start_like_commands_are_translated(self):
        for sample in ["!play something nice please", "...and then he left", "?", "?? what happened"]:
            with self.subTest(sample=sample):
                self.assertNotEqual(get_skip_reason(sample), "command")

    def test_too_long(self):
        self.assertEqual(get_skip_reason("word " * 400), "too_long")

    def test_long_code_block_with_short_text_is_not_too_long(self):
        message = "Why does this fail?\n```\n" + ("x = 1\n" * 400) + "```"
        self.assertIsNone(get_skip_reason(message))

    def test_mixed_text_and_code_block_is_translated(self):
        self.assertIsNone(get_skip_reason("Can someone help?\n```js\nconst a = 1;\n```"))


class ProtectRestoreTests(unittest.TestCase):
    def test_round_trip_keeps_protected_parts(self):
        text = (
            "Hey <@123456789012345678>, check https://example.com/a?b=1 and run `npm i` 😀\n"
            "```js\nconst a = 1;\n```\nThanks <:pepe:123456789012345678> <t:1700000000:R>"
        )
        protected, segments = protect_segments(text)
        for part in ["<@123456789012345678>", "https://example.com/a?b=1", "`npm i`", "😀",
                     "```js\nconst a = 1;\n```", "<:pepe:123456789012345678>", "<t:1700000000:R>"]:
            self.assertIn(part, segments)
            self.assertNotIn(part, protected)
        self.assertEqual(restore_segments(protected, segments), text)

    def test_restores_markers_with_spaces_added_by_translator(self):
        _, segments = protect_segments("see https://a.io now")
        self.assertEqual(restore_segments("žiūrėk { 0 } dabar", segments), "žiūrėk https://a.io dabar")

    def test_lost_markers_are_appended(self):
        _, segments = protect_segments("look https://a.io and <@123456789012345678>")
        self.assertEqual(
            restore_segments("žiūrėk {0}", segments),
            "žiūrėk https://a.io <@123456789012345678>",
        )

    def test_literal_braces_in_text_survive(self):
        text = "use {0} as placeholder and see https://a.io"
        protected, segments = protect_segments(text)
        self.assertEqual(restore_segments(protected, segments), text)

    def test_spoilers_stay_hidden(self):
        # Madlad dropped the "||" and revealed the spoiler, so spoilers are kept as-is.
        text = "The killer is ||the butler||, don't tell anyone!"
        protected, segments = protect_segments(text)
        self.assertIn("||the butler||", segments)
        self.assertNotIn("butler", protected)
        self.assertEqual(restore_segments(protected, segments), text)

    def test_heading_and_quote_prefixes_are_kept_and_text_is_translated(self):
        text = "# Rules\n-# small print\n> Have fun\n>>> long quote"
        protected, segments = protect_segments(text)
        self.assertEqual(protected, "{0} Rules\n{1} small print\n{2} Have fun\n{3} long quote")
        self.assertEqual(restore_segments(protected, segments), text)

    def test_hashtag_is_not_a_heading(self):
        protected, _ = protect_segments("#general is the best channel")
        self.assertEqual(protected, "#general is the best channel")

    def test_emoji_sequences_stay_whole(self):
        text = "family 👨‍👩‍👧 and thumbs 👍🏽"
        protected, segments = protect_segments(text)
        self.assertIn("👨‍👩‍👧", segments)
        self.assertIn("👍🏽", segments)
        self.assertEqual(restore_segments(protected, segments), text)


class AdversarialInputTests(unittest.TestCase):
    def test_filter_is_fast_on_pathological_messages(self):
        # "import" + spaces used to take ~6 s (catastrophic regex backtracking),
        # freezing the whole bot because on_message runs the filter on every message.
        samples = [
            "import" + " " * 3994, "import " * 570, ":" * 4000, "`" * 4000, "<" * 4000,
            "{ " + "1" * 3998, "function a(" * 360, "def a(" * 660, "x;\n" * 1333,
            "a {" + " b:" * 1330, "😀‍" * 2000, "$a " * 1333, "if (" * 1000,
        ]
        for sample in samples:
            started = time.perf_counter()
            get_skip_reason(sample)
            protect_segments(sample)
            looks_like_code(sample)
            with self.subTest(sample=sample[:20]):
                self.assertLess(time.perf_counter() - started, 0.5)


class SplitForDiscordTests(unittest.TestCase):
    def test_short_text_is_one_message(self):
        self.assertEqual(split_for_discord("hello"), ["hello"])

    def test_long_text_is_split_without_losing_words(self):
        text = "\n".join(f"Line number {i} with some words" for i in range(200))
        parts = split_for_discord(text)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= 2000 for p in parts))
        self.assertEqual("\n".join(parts), text)

    def test_single_huge_line_is_wrapped(self):
        text = "lol " * 600
        parts = split_for_discord(text)
        self.assertTrue(all(len(p) <= 2000 for p in parts))
        self.assertEqual(" ".join(parts).split(), text.split())

    def test_code_block_is_closed_and_reopened(self):
        body = "\n".join(f"const v{i} = {i};" for i in range(200))
        text = f"Look:\n```js\n{body}\n```\nDone"
        parts = split_for_discord(text)
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(len(part), 2000)
            self.assertEqual(part.count("```") % 2, 0)
        self.assertTrue(parts[1].startswith("```js"))
        joined_code = "\n".join(parts)
        for i in range(200):
            self.assertIn(f"const v{i} = {i};", joined_code)


if __name__ == "__main__":
    unittest.main()
