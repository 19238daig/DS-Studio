SET LOCAL ROLE ds_olist_owner;
CREATE VIEW olist.v_order_items_agg WITH(security_invoker=true) AS
 SELECT order_id,count(*) AS item_count,sum(price) AS item_amount,sum(freight_value) AS freight_amount
 FROM olist.order_items GROUP BY order_id;
CREATE VIEW olist.v_order_payments_agg WITH(security_invoker=true) AS
 SELECT order_id,count(*) AS payment_count,sum(payment_value) AS payment_amount
 FROM olist.order_payments GROUP BY order_id;
CREATE VIEW olist.v_order_reviews_agg WITH(security_invoker=true) AS
 SELECT order_id,count(*) AS review_count,avg(review_score) AS average_review_score
 FROM olist.order_reviews GROUP BY order_id;
CREATE VIEW olist.v_order_summary WITH(security_invoker=true) AS
 SELECT o.order_id,o.customer_id,c.customer_unique_id,c.customer_city,c.customer_state,
 o.order_status,o.order_purchase_timestamp,o.order_delivered_customer_date,o.order_estimated_delivery_date,
 coalesce(i.item_count,0) AS item_count,coalesce(i.item_amount,0::numeric) AS item_amount,
 coalesce(i.freight_amount,0::numeric) AS freight_amount,
 coalesce(p.payment_count,0) AS payment_count,coalesce(p.payment_amount,0::numeric) AS payment_amount,
 coalesce(r.review_count,0) AS review_count,r.average_review_score
 FROM olist.orders o JOIN olist.customers c USING(customer_id)
 LEFT JOIN olist.v_order_items_agg i USING(order_id)
 LEFT JOIN olist.v_order_payments_agg p USING(order_id)
 LEFT JOIN olist.v_order_reviews_agg r USING(order_id);
INSERT INTO ds_meta.schema_migrations(version) VALUES(2);
RESET ROLE;
