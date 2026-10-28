"""A task's parallel tool calls must not race its bounded parse cache."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import threading

import pandas as pd
import pytest

from src.config import Settings
from src.data import DataService
from src.storage import Store
import src.storage as storage


@pytest.fixture
def cached_data(tmp_path):
    store = Store(Settings(workspace=tmp_path, _env_file=None))
    sid = store.new_session()['session_id']
    files = []
    for i in range(5):
        path = store.root / 'KB' / f'{i}.csv'
        path.write_text(f'value\n{i}\n', encoding='utf-8')
        files.append(store.register_file(path, path.name, None, source='kb')['file_id'])
    return DataService(store), sid, files


def test_cached_lookup_is_atomic_with_eviction_and_still_hashes(cached_data, monkeypatch):
    data, sid, files = cached_data
    data.read_table(files[0], sid)
    original_hash = storage.file_hash
    hashes = []

    def counted_hash(path):
        hashes.append((threading.current_thread().name, path.name))
        return original_hash(path)

    monkeypatch.setattr(storage, 'file_hash', counted_hash)
    checked, release = threading.Event(), threading.Event()

    class ScheduledLRU(OrderedDict):
        def __contains__(self, key):
            present = super().__contains__(key)
            if present and threading.current_thread().name.startswith('cache-reader'):
                checked.set()
                assert release.wait(5)
            return present

    data._parsed = ScheduledLRU(data._parsed)
    with ThreadPoolExecutor(1, thread_name_prefix='cache-reader') as readers, ThreadPoolExecutor(1, thread_name_prefix='cache-writer') as writers:
        reader = readers.submit(data.read_table, files[0], sid)
        assert checked.wait(5)
        writer = writers.submit(lambda: [data.read_table(fid, sid) for fid in files[1:4]])
        try:
            # The writer must not evict while lookup owns a reference-in-flight.
            with pytest.raises(TimeoutError):
                writer.result(timeout=.2)
        finally:
            release.set()
        assert int(reader.result(timeout=5).iloc[0, 0]) == 0
        writer.result(timeout=5)
    assert sum(name.startswith('cache-reader') for name, _ in hashes) == 1
    assert sum(name.startswith('cache-writer') for name, _ in hashes) == 6
    assert len(data._parsed) == 3
    assert data._parsed_bytes == sum(size for _, size in data._parsed.values())


def test_concurrent_misses_same_key_do_not_double_count_bytes(cached_data, monkeypatch):
    data, sid, files = cached_data
    original_load = data._load_uncached
    parsing = threading.Barrier(4)

    def simultaneous_parse(*args):
        # All threads passed the empty lookup. Parsing must run outside the LRU
        # lock, otherwise this barrier times out and exposes serialization.
        parsing.wait(timeout=5)
        return original_load(*args)

    monkeypatch.setattr(data, '_load_uncached', simultaneous_parse)
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: data.read_table(files[0], sid), range(4)))
    assert [int(frame.iloc[0, 0]) for frame in results] == [0] * 4
    assert len(data._parsed) == 1
    assert data._parsed_bytes == next(iter(data._parsed.values()))[1]
    monkeypatch.setattr(data, '_load_uncached', original_load)
    for fid in files[1:]:
        data.read_table(fid, sid)
    assert len(data._parsed) == 3
    assert 0 < data._parsed_bytes == sum(size for _, size in data._parsed.values()) <= 64 * 1024 * 1024


def test_parallel_inserts_enforce_byte_limit(cached_data, monkeypatch):
    data, sid, files = cached_data
    original_load = data._load_uncached
    parsing = threading.Barrier(3)

    def simultaneous_parse(*args):
        parsing.wait(timeout=5)
        return original_load(*args)

    monkeypatch.setattr(data, '_load_uncached', simultaneous_parse)
    # Exercise the byte-eviction path without allocating 100 MiB in a unit test.
    monkeypatch.setattr(pd.DataFrame, 'memory_usage', lambda *a, **k: pd.Series([33 * 1024 * 1024]))
    with ThreadPoolExecutor(3) as pool:
        frames = list(pool.map(lambda fid: data.read_table(fid, sid), files[:3]))
    assert [int(frame.iloc[0, 0]) for frame in frames] == [0, 1, 2]
    assert len(data._parsed) == 1
    assert data._parsed_bytes == sum(size for _, size in data._parsed.values())
    assert 33 * 1024 * 1024 < data._parsed_bytes <= 64 * 1024 * 1024
