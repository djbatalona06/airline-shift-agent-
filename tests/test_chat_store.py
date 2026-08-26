"""Chat history persistence.

Server-side history is what makes "reopen the dashboard and the thread is still
there" true without depending on a cookie surviving, so these are the tests
behind that claim.
"""

from __future__ import annotations


def test_appends_and_reads_back_in_order(chat_store_and_config):
    store, config = chat_store_and_config
    store.append_message("tester", "user", "dashboard", "first")
    store.append_message("tester", "agent", "dashboard", "second")

    rows = store.messages_after("tester")

    assert [r["text"] for r in rows] == ["first", "second"]
    assert [r["role"] for r in rows] == ["user", "agent"]


def test_append_returns_the_new_id(chat_store_and_config):
    store, _ = chat_store_and_config
    first = store.append_message("tester", "user", "dashboard", "a")
    second = store.append_message("tester", "user", "dashboard", "b")
    assert second > first


def test_messages_after_is_a_cursor(chat_store_and_config):
    """The dashboard polls with this; returning already-seen rows would
    duplicate every message in the log."""
    store, _ = chat_store_and_config
    first = store.append_message("tester", "user", "dashboard", "old")
    store.append_message("tester", "agent", "telegram", "new")

    rows = store.messages_after("tester", after_id=first)

    assert [r["text"] for r in rows] == ["new"]


def test_history_is_per_profile(chat_store_and_config):
    """Two people sharing one machine must not see each other's conversation."""
    store, _ = chat_store_and_config
    store.append_message("tester", "user", "dashboard", "mine")
    store.append_message("someone-else", "user", "dashboard", "theirs")

    assert [r["text"] for r in store.messages_after("tester")] == ["mine"]


def test_recent_messages_returns_the_newest_in_chronological_order(chat_store_and_config):
    """The model needs the most recent context, oldest-first within that window."""
    store, _ = chat_store_and_config
    for n in range(10):
        store.append_message("tester", "user", "dashboard", f"m{n}")

    rows = store.recent_messages("tester", limit=3)

    assert [r["text"] for r in rows] == ["m7", "m8", "m9"]


def test_survives_reopening_the_database(tmp_path, chat_store_and_config):
    """The reopen-the-dashboard case, directly."""
    from shift_agent.store import Store

    store, _ = chat_store_and_config
    store.append_message("tester", "user", "dashboard", "still here")
    path = store.path
    store.close()

    reopened = Store(path)
    try:
        assert [r["text"] for r in reopened.messages_after("tester")] == ["still here"]
    finally:
        reopened.close()


# --- threading ---------------------------------------------------------------
# The dashboard server answers on its own handler threads, so the connection is
# reached from more than the poll loop's thread. sqlite3's default guard only
# checks which thread *built* the connection, which was never the property that
# mattered; store.py replaces it with a lock.

def test_readable_from_another_thread(chat_store_and_config):
    import threading

    store, _ = chat_store_and_config
    store.append_message("tester", "user", "dashboard", "written on the main thread")
    seen = []

    thread = threading.Thread(target=lambda: seen.extend(store.messages_after("tester")))
    thread.start()
    thread.join()

    assert [r["text"] for r in seen] == ["written on the main thread"]


def test_concurrent_writers_do_not_lose_rows(chat_store_and_config):
    import threading

    store, _ = chat_store_and_config

    def write(n):
        for i in range(20):
            store.append_message("tester", "user", "dashboard", f"{n}-{i}")

    threads = [threading.Thread(target=write, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(store.messages_after("tester")) == 80
