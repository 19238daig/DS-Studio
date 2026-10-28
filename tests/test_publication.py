"""Publish what was reviewed; never include local credentials or analysis data."""
import subprocess

from scripts.check_publication import audit


def run(root, *args):
    return subprocess.run(['git','-C',str(root),*args],check=True,capture_output=True)


def repository(tmp_path):
    run(tmp_path,'init')
    for name in ('README.md','LICENSE','THIRD_PARTY_NOTICES.md','.env.example'):
        (tmp_path/name).write_text('Public template\n',encoding='utf-8')
    run(tmp_path,'add','.')
    return tmp_path


def test_publication_audits_staged_content_even_after_worktree_is_cleaned(tmp_path):
    root=repository(tmp_path)
    # Construct an obviously fictitious key at runtime; do not put token-shaped
    # literals in the public source tree itself.
    secret='sk-'+'1'*32
    (root/'app.py').write_text('key='+repr(secret),encoding='utf-8')
    run(root,'add','app.py')
    (root/'app.py').write_text('key=""',encoding='utf-8')
    result=audit(root)
    assert not result['ok']
    assert {'path':'app.py','reason':'provider_key'} in result['issues']
    assert secret not in str(result)


def test_user_data_and_runtime_material_are_rejected(tmp_path):
    root=repository(tmp_path)
    (root/'KB').mkdir();(root/'KB'/'private.csv').write_text('private\n',encoding='utf-8')
    (root/'.env').write_text('API_KEY=local-test-credential',encoding='utf-8')
    run(root,'add','.')
    result=audit(root)
    assert not result['ok']
    assert any(x['path']=='KB/private.csv' for x in result['issues'])
    assert any(x['path']=='.env' for x in result['issues'])


def test_upstream_design_catalog_allowed_but_unrelated_csv_rejected(tmp_path):
    root=repository(tmp_path)
    folder=root/'skills/design/ui-ux-pro-max/data';folder.mkdir(parents=True)
    (folder/'colors.csv').write_text('name,hex\nbrand,#3D5BC7',encoding='utf-8')
    run(root,'add','.')
    result=audit(root)
    assert result['ok'] and result['design_reference_csv_count']==1
    (root/'customers.csv').write_text('customer\n',encoding='utf-8')
    run(root,'add','customers.csv')
    assert not audit(root)['ok']


def test_untracked_local_secret_is_detected_if_copied_into_staged_source(tmp_path):
    root=repository(tmp_path)
    secret='private-local-only-credential'
    (root/'.env').write_text('API_KEY='+secret,encoding='utf-8')
    (root/'app.py').write_text('value='+repr(secret),encoding='utf-8')
    run(root,'add','app.py')
    result=audit(root)
    assert any(x['reason']=='local_secret_value' for x in result['issues'])
    assert secret not in str(result)


def test_short_password_checks_credential_context_not_ordinary_identifiers(tmp_path):
    root=repository(tmp_path)
    word='post'+'gres'
    (root/'.env').write_text('DB_ADMIN_PASSWORD='+word,encoding='utf-8')
    (root/'app.py').write_text('database_user='+repr(word),encoding='utf-8')
    run(root,'add','app.py')
    assert audit(root)['ok']
    (root/'app.py').write_text('database_password='+repr(word),encoding='utf-8')
    run(root,'add','app.py')
    assert any(x['reason']=='local_secret_value' for x in audit(root)['issues'])
