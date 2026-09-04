from bot import is_emoji_only_message


def test_emoji_only_message_returns_true():
    assert is_emoji_only_message("😀😎") is True


def test_mixed_text_and_emoji_returns_false():
    assert is_emoji_only_message("hello 😀") is False


def test_empty_message_returns_true():
    assert is_emoji_only_message("") is True
