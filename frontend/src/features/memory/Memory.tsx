import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { App, Button, List, Modal, Select, Tag } from 'antd';
import { DeleteOutlined } from '@ant-design/icons';
import { api } from '../../shared/api/client';
import { Blank, date, ErrorNotice, errorMessage, Loading } from '../../shared/ui';
export default function Memory() {
  const [role, setRole] = useState('analyst');
  const { message } = App.useApp();
  const client = useQueryClient();
  const [confirm, setConfirm] = useState<string>();
  const [busy, setBusy] = useState(false);
  const records = useQuery({ queryKey: ['memory', role], queryFn: () => api.memories(role) });
  async function remove() {
    if (!confirm) return;
    setBusy(true);
    try {
      await api.deleteMemory(role, confirm);
      setConfirm(undefined);
      await client.invalidateQueries({ queryKey: ['memory', role] });
      void message.success('该条记忆已删除。');
    } catch (e) {
      void message.error(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="ds-page-content">
      <div className="ds-page-heading">
        <div>
          <h2>Agent 记忆</h2>
          <p>两位专家独立保存经验与偏好，删除仅影响选中条目。</p>
        </div>
        <Select
          aria-label="选择专家记忆"
          value={role}
          onChange={setRole}
          options={[
            { value: 'analyst', label: '数据分析 Agent' },
            { value: 'report_designer', label: '报告生成 Agent' },
          ]}
        />
      </div>
      {records.isPending ? (
        <Loading />
      ) : records.error ? (
        <ErrorNotice error={records.error} />
      ) : records.data?.items.filter((m) => m.status !== 'deleted').length ? (
        <List
          dataSource={records.data.items.filter((m) => m.status !== 'deleted')}
          renderItem={(m) => (
            <List.Item
              actions={[
                <Button
                  key="delete"
                  aria-label="删除这条记忆"
                  icon={<DeleteOutlined />}
                  onClick={() => setConfirm(m.memory_id)}
                />,
              ]}
            >
              <List.Item.Meta
                title={
                  <>
                    <Tag>{m.status === 'active' ? '已生效' : '候选'}</Tag>
                    {m.kind}
                    <small className="ds-hint"> {date(m.created_at)}</small>
                  </>
                }
                description={<p className="ds-memory-content">{m.content}</p>}
              />
            </List.Item>
          )}
        />
      ) : (
        <Blank title="这位专家还没有记忆记录" />
      )}
      <Modal
        title="删除这条专家记忆？"
        open={!!confirm}
        onCancel={() => setConfirm(undefined)}
        onOk={remove}
        confirmLoading={busy}
        okText="删除记忆"
        cancelText="保留"
        okButtonProps={{ danger: true }}
      >
        <p>会话、数据和已生成报告不受影响。</p>
      </Modal>
    </div>
  );
}
