from codex_telegram_bridge.exec_bridge import extract_session_id


def test_extract_session_id_finds_uuid_v7() -> None:
    uuid = "019b66fc-64c2-7a71-81cd-081c504cfeb2"
    text = f"resume session `{uuid}` please"

    assert extract_session_id(text) == uuid

