# Olist PostgreSQL 数据源

一期使用 `E:/testing_dataset/Brazilian E-Commerce Public Dataset by Olist` 七份 CSV；原始文件及桌面 SQL 原件保持不变。管理端使用客户端 COPY，不依赖 PostgreSQL 服务账号读取 E 盘。

`olist` 是只读全量分析身份，`sellers_restricted` 是独立卖家城市身份，只能访问 sellers 中 `seller_city='santo andre'` 的行。这里的单用户本地配置不构成 HTTP 多租户隔离。不要将受限卖家结果与全量快照混用；订单没有自动继承卖家的行级权限。

客户数用 customer_unique_id 去重；customer_id 是订单关联键。商品金额、运费与支付金额是不同口径；订单状态与日期范围应明确。三张明细表先分别按订单聚合再连接，避免金额扇出。四个视图均为 security_invoker。NUMERIC 保留精度，时间字段是原始无时区时间。

产品 CSV 原始拼写 product_name_lenght/product_description_lenght 统一映射到 product_name_length/product_description_length。缺失值按 NULL 保存，不自动删除、填补或去重。类别翻译、地理数据不在一期导入范围内。

管理命令：`.venv/Scripts/python.exe scripts/db_manage.py status|bootstrap|migrate|import`。连接及密码仅放本机 .env；禁止将密码复制到 SQL、报告、日志或 Agent 上下文。
