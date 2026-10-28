# 数据库迁移与导入

这里使用版本化迁移及本机管理 CLI。个人 SQL 脚本和数据库文件不随开源代码分发，也不会自动执行。

1. 本机 .env 配置 DB_HOST / DB_PORT / DB_NAME / DB_ADMIN_USER / DB_ADMIN_PASSWORD。
2. `python scripts/db_manage.py status` 只读盘点目标库。
3. `python scripts/db_manage.py bootstrap` 只创建缺失库与项目对象，并生成两种独立只读身份。已有同名非项目角色、schema 或视图冲突会拒绝；不重置旧角色密码。
4. 将 `DB_DATASET_DIR` 配置为自行准备的 Olist CSV 目录（示例 `./datasets/olist`），再运行 `python scripts/db_manage.py import` 加载七份 CSV。客户端 STDIN COPY 不要求服务器能访问本机路径；数据集不在仓库中。
5. 再次 import 对同哈希批次做数量核对后跳过；不同文件且业务表非空会拒绝。无 TRUNCATE、删除业务表、去重或覆盖策略。

迁移文件只应由管理 CLI 在所有权检查后执行。七表按依赖顺序加载，列映射、类型精度、候选键、外键、缺失数量和文件前后 SHA-256 均核对；整批和批次登记在一个事务提交。失败时整批回滚，修正文件后重试。导入后 ANALYZE 更新查询统计。

管理身份与查询身份分离；后台管理凭据不提供给 Agent 或 Docker。受限角色只获得 sellers SELECT 与指定城市 RLS，不拥有业务对象，也不继承全量角色。
