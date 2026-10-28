"""Delivered reports stay with their own dialogue turn; previews load on demand."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from src.client import ApiClient
from test_ui import file_ui


@pytest.fixture
def report_chat(file_ui, monkeypatch):
    import streamlit.components.v1 as components
    at, _ = file_ui
    def artifact(aid, tid, suffix='.html'):
        return {'artifact_id':aid, 'task_id':tid, 'session_id':'s1', 'kind':'report',
                'name':'report'+suffix, 'mime_type':'text/html' if suffix=='.html' else 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                'size':2048, 'downloadable':True}
    original=artifact('original','t1')
    revised=artifact('revised','t2')
    workbook=artifact('workbook','t2','.xlsx')
    records={tid:{'task_id':tid,'status':'succeeded','spec':{},'result':result}
             for tid,result in [('t1',{'report_ids':['original']}),
                               ('t2',{'report_ids':['revised','workbook']}),
                               ('t3',{'analysis_ids':['new-analysis'],'report_ids':[]})]}
    session={'session_id':'s1','tasks':['t1','t2','t3'],
        'messages':[{'role':role,'content':content,'task_id':tid} for tid in ('t1','t2','t3')
                    for role,content in [('user','原报告' if tid=='t1' else '修改报告' if tid=='t2' else '新问题'),
                                         ('assistant','报告已生成。' if tid!='t3' else '新问题的结论。')]],
        'report_versions':[
            {'task_id':'t2','number':2,'version':'v2','title':'修改后的销售报告','request':'修改报告',
             'created_at':'2026-10-27T02:00:00Z','base_number':1,'revision_of':{'report_id':'original'},'artifacts':[revised,workbook]},
            {'task_id':'t1','number':1,'version':'v1','title':'原始销售报告','request':'原报告',
             'created_at':'2026-10-27T01:00:00Z','revision_of':None,'artifacts':[original]}]}
    monkeypatch.setattr(ApiClient,'session',lambda self,sid:deepcopy(session))
    monkeypatch.setattr(ApiClient,'task',lambda self,tid:deepcopy(records[tid]))
    monkeypatch.setattr(ApiClient,'events',lambda self,tid,after=0:{'items':[],'cursor':0})
    artifacts=Mock(side_effect=lambda tid: [original] if tid=='t1' else [revised,workbook] if tid=='t2' else [])
    monkeypatch.setattr(ApiClient,'artifacts',lambda self,tid:artifacts(tid))
    download=Mock(side_effect=lambda aid:f'<html><body>{aid}</body></html>'.encode())
    monkeypatch.setattr(ApiClient,'download',lambda self,aid:download(aid))
    submit=Mock(side_effect=AssertionError('Attachment actions must not create model tasks'))
    monkeypatch.setattr(ApiClient,'submit',submit)
    embed=Mock()
    monkeypatch.setattr(components,'html',embed)
    return at,session,records,download,submit,embed,artifacts


def test_reports_attach_to_their_own_responses_even_after_a_new_analysis(report_chat):
    at,_,_,download,submit,embed,artifacts=report_chat
    at.run()
    assert not at.exception and not at.error
    chats=at.chat_message
    assert len(chats)==6
    assert {b.key for b in chats[1].button}=={'chat_preview_t1_original','chat_open_t1_original'}
    assert {b.key for b in chats[3].button}=={'chat_preview_t2_revised','chat_open_t2_revised','chat_open_t2_workbook'}
    assert not chats[5].button
    assert any('原始销售报告' in m.value for m in chats[1].markdown)
    assert any('修改后的销售报告' in m.value for m in chats[3].markdown)
    assert any('基于版本 1 修改' in c.value for c in chats[3].caption)
    assert all(call.args==('t3',) for call in artifacts.call_args_list)
    download.assert_not_called()
    submit.assert_not_called()
    embed.assert_not_called()


def test_inline_preview_is_lazy_cached_and_does_not_navigate_or_generate(report_chat):
    at,_,_,download,submit,embed,_=report_chat
    at.run().button(key='chat_preview_t1_original').click().run()
    assert not at.exception and not at.error
    assert at.session_state.workspace_tabs=='对话'
    download.assert_called_once_with('original')
    assert embed.call_count==1
    assert 'original' in embed.call_args.args[0]
    at.button(key='chat_preview_t1_original').click().run()
    download.assert_called_once_with('original')
    embed.reset_mock()
    at.button(key='chat_preview_t2_revised').click().run()
    assert not at.exception and not at.error
    assert [call.args[0] for call in download.call_args_list]==['original','revised']
    assert embed.call_count==1 and 'revised' in embed.call_args.args[0]
    assert not any(b.key=='chat_close_t1_original' for b in at.button)
    at.button(key='chat_close_t2_revised').click().run()
    assert 'conversation_report_preview' not in at.session_state
    submit.assert_not_called()


def test_same_reply_text_does_not_hide_the_new_report_turn(report_chat):
    at,session,records,_,_,_,_=report_chat
    session['tasks']=['t1','t2']
    session['messages']=session['messages'][:3]
    records['t2']['final_response']='报告已生成。'
    at.run()
    assert not at.exception and not at.error
    assert len(at.chat_message)==4
    assert at.chat_message[1].button(key='chat_preview_t1_original')
    assert at.chat_message[3].button(key='chat_preview_t2_revised')


def test_report_catalog_cannot_attach_foreign_or_non_report_artifacts(report_chat):
    at,session,_,download,_,_,_=report_chat
    version=session['report_versions'][0]
    version['artifacts'] += [
        {**version['artifacts'][0],'artifact_id':'foreign','session_id':'another-session'},
        {**version['artifacts'][0],'artifact_id':'wrong-task','task_id':'t1'},
        {**version['artifacts'][0],'artifact_id':'csv','name':'data.csv','kind':'table'},
    ]
    session['report_versions'].append({**version,'task_id':'not-in-session', 'artifacts':[
        {**version['artifacts'][0],'artifact_id':'foreign-task'}]})
    at.run()
    assert not at.exception and not at.error
    keys={b.key for chat in at.chat_message for b in chat.button}
    assert not any(key and any(value in key for value in ('foreign','wrong-task','_csv')) for key in keys)
    download.assert_not_called()
