"""Real source-file coverage for knowledge-base document material."""
import pytest

from src.config import Settings
from src.data import DataService
from src.jobs import dispatch_task
from src.schemas import TaskSpec
from src.storage import Store, StoreError


@pytest.fixture
def material_store(tmp_path):
    store = Store(Settings(workspace=tmp_path, _env_file=None))
    sid = store.new_session()['session_id']
    return store, sid, DataService(store)


def add_material(store, name, content, encoding='utf-8'):
    path = store.root / 'KB' / name
    path.write_bytes(content.encode(encoding))
    item = next(f for f in store.refresh_kb() if f['name'] == name)
    return path, item['file_id']


@pytest.mark.parametrize('name,encoding', [('资料.md', 'utf-8-sig'), ('资料.txt', 'gb18030')])
def test_material_literal_text_and_no_fabricated_table(material_store, name, encoding):
    store, sid, data = material_store
    source = '# 中文材料\n编号 001；缺失标记 NA。\n|项目|数量|\n|甲|5|\n<script>not_executed()</script>'
    _, fid = add_material(store, name, source, encoding)
    result = data.read_text(fid, sid)
    assert result == dict(file_id=fid, text=source, total_characters=len(source), truncated=False)
    profiles = data.profiles(fid, sid)
    assert len(profiles) == 1
    profile = profiles[0]
    assert profile.row_count == 0 and profile.sheet_or_table is None
    assert profile.columns == [] and profile.preview == [] and profile.dtypes == {}
    assert profile.file_hash == store.get_file(fid, sid)['hash']
    assert any('no tables inferred' in warning for warning in profile.quality_warnings)
    with pytest.raises(StoreError) as exc:
        data.read_table(fid, sid)
    assert exc.value.code == 'no_tables'


def test_material_truncation_empty_and_bounds(material_store):
    store, sid, data = material_store
    source = '中文摘要。' * 20
    _, fid = add_material(store, 'long.md', source)
    result = data.read_text(fid, sid, max_chars=7)
    assert result['text'] == source[:7]
    assert result['total_characters'] == len(source) and result['truncated']
    for limit in (0, 50001):
        with pytest.raises(StoreError):
            data.read_text(fid, sid, max_chars=limit)
    _, empty = add_material(store, 'empty.txt', '')
    assert data.read_text(empty, sid)['text'] == ''
    assert data.read_text(empty, sid)['total_characters'] == 0
    assert data.profile(empty, sid).row_count == 0


def test_material_source_hash_and_size_are_enforced(material_store):
    store, sid, data = material_store
    path, fid = add_material(store, 'source.md', 'original')
    path.write_text('changed', encoding='utf-8')
    with pytest.raises(StoreError) as exc:
        data.read_text(fid, sid)
    assert exc.value.code == 'input_changed'
    _, updated = add_material(store, 'source.md', 'abcdefgh')
    store.settings.max_upload_bytes = 4
    with pytest.raises(StoreError) as exc:
        data.profiles(updated, sid)
    assert exc.value.code == 'input_too_large'


@pytest.mark.parametrize('raw,code', [(b'\xff', 'encoding_error'), (b'plain\x00text', 'invalid_text')])
def test_material_invalid_text_is_rejected(material_store, raw, code):
    store, sid, data = material_store
    path = store.root / 'KB' / 'bad.txt'
    path.write_bytes(raw)
    fid = store.refresh_kb()[0]['file_id']
    with pytest.raises(StoreError) as exc:
        data.read_text(fid, sid)
    assert exc.value.code == code


def test_mixed_kb_refresh_keeps_documents_available(material_store):
    store, sid, data = material_store
    add_material(store, 'guide.md', '# 分析口径\n仅用于说明，不含统计表。')
    add_material(store, 'notes.txt', '中文备注')
    (store.root / 'KB' / 'numbers.csv').write_text('value\n2\n4\n', encoding='utf-8')
    task = TaskSpec(session_id=sid, request='refresh', intent='kb_refresh')
    store.create_task(task, kind='kb_refresh')
    result = dispatch_task(task, store, lambda *args, **kwargs: None, lambda: False)
    assert result['status'] == 'completed' and result['errors'] == {}
    files = {f['name']: f for f in store.list_files(sid)}
    assert set(files) == {'guide.md', 'notes.txt', 'numbers.csv'}
    assert files['numbers.csv']['profile_status'] == 'ready'
    assert files['guide.md']['profile_status'] == 'ready'
    assert files['notes.txt']['profile_status'] == 'ready'
    assert data.read_text(files['guide.md']['file_id'], sid)['text'].startswith('# 分析口径')
    assert data.profile(files['notes.txt']['file_id'], sid).columns == []


def test_static_html_material_still_filters_active_content(material_store):
    store, sid, data = material_store
    _, fid = add_material(store, 'page.html', '<p>中文材料</p><script>hidden()</script><style>secret</style><img src="https://invalid.local/image">')
    assert data.read_text(fid, sid)['text'] == '中文材料'
    assert data.profile(fid, sid).columns == []
