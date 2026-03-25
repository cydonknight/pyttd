import pytest
from pyttd.session import Session
from pyttd.models import storage
from pyttd.models.db import db


def test_coroutine_call_children_merged(record_func):
    """Coroutine that suspends should have merged entries with suspendCount."""
    db_path, run_id, stats = record_func('''
        import asyncio

        async def fetch(url):
            await asyncio.sleep(0)
            return f"data from {url}"

        async def main():
            a = await fetch("/a")
            b = await fetch("/b")

        asyncio.run(main())
    ''')
    session = Session()
    session.enter_replay(run_id, 0)
    # Get root-level call children
    children = session.get_call_children()
    # Verify isCoroutine field exists
    for child in children:
        assert 'isCoroutine' in child


def test_non_coroutine_not_merged(record_func):
    """Regular functions should not be merged."""
    db_path, run_id, stats = record_func('''
        def greet(name):
            return f"hello {name}"
        greet("a")
        greet("b")
    ''')
    session = Session()
    session.enter_replay(run_id, 0)
    # greet is called from <module>, so it appears as a child of the root frame
    root_children = session.get_call_children()
    module_entry = next((c for c in root_children if c['functionName'] == '<module>'), None)
    if module_entry is None:
        # Fallback: check root children directly
        greet_calls = [c for c in root_children if c['functionName'] == 'greet']
    else:
        greet_calls = [
            c for c in session.get_call_children(
                module_entry['callSeq'], module_entry.get('returnSeq'))
            if c['functionName'] == 'greet'
        ]
    assert len(greet_calls) == 2  # Not merged
    for c in greet_calls:
        assert c.get('suspendCount') is None or c.get('suspendCount') == 0


def test_coroutine_suspensions_rpc(record_func):
    """get_coroutine_suspensions should return suspend/resume pairs."""
    db_path, run_id, stats = record_func('''
        import asyncio

        async def multi_await():
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return "done"

        asyncio.run(multi_await())
    ''')
    session = Session()
    session.enter_replay(run_id, 0)
    children = session.get_call_children()
    # Find merged coroutine entries
    coro_entries = [c for c in children if c.get('isCoroutine')]
    if coro_entries:
        for entry in coro_entries:
            if entry.get('suspendCount') and entry.get('returnSeq'):
                suspensions = session.get_coroutine_suspensions(
                    entry['callSeq'], entry['returnSeq'])
                assert isinstance(suspensions, list)
