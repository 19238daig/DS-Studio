-- DS Project Olist schema v1; execute only after importer ownership checks.
CREATE SCHEMA olist AUTHORIZATION ds_olist_owner;
COMMENT ON SCHEMA olist IS 'ds_project:olist:v1';
CREATE SCHEMA ds_meta AUTHORIZATION ds_olist_owner;
COMMENT ON SCHEMA ds_meta IS 'ds_project:metadata:v1';
SET LOCAL ROLE ds_olist_owner;
CREATE TABLE olist.customers (
 customer_id text PRIMARY KEY, customer_unique_id text NOT NULL,
 customer_zip_code_prefix text, customer_city text, customer_state text);
CREATE TABLE olist.products (
 product_id text PRIMARY KEY, product_category_name text, product_name_length integer,
 product_description_length integer, product_photos_qty integer, product_weight_g integer,
 product_length_cm integer, product_height_cm integer, product_width_cm integer);
CREATE TABLE olist.sellers (
 seller_id text PRIMARY KEY, seller_zip_code_prefix text, seller_city text, seller_state text);
CREATE TABLE olist.orders (
 order_id text PRIMARY KEY, customer_id text NOT NULL REFERENCES olist.customers(customer_id),
 order_status text NOT NULL, order_purchase_timestamp timestamp,
 order_approved_at timestamp, order_delivered_carrier_date timestamp,
 order_delivered_customer_date timestamp, order_estimated_delivery_date timestamp);
CREATE TABLE olist.order_items (
 order_id text NOT NULL REFERENCES olist.orders(order_id), order_item_id integer NOT NULL,
 product_id text NOT NULL REFERENCES olist.products(product_id),
 seller_id text NOT NULL REFERENCES olist.sellers(seller_id), shipping_limit_date timestamp,
 price numeric(18,2) NOT NULL, freight_value numeric(18,2) NOT NULL,
 PRIMARY KEY(order_id,order_item_id), CHECK(price>=0), CHECK(freight_value>=0));
CREATE TABLE olist.order_payments (
 order_id text NOT NULL REFERENCES olist.orders(order_id), payment_sequential integer NOT NULL,
 payment_type text, payment_installments integer, payment_value numeric(18,2) NOT NULL,
 PRIMARY KEY(order_id,payment_sequential), CHECK(payment_value>=0));
CREATE TABLE olist.order_reviews (
 review_id text NOT NULL, order_id text NOT NULL REFERENCES olist.orders(order_id),
 review_score integer NOT NULL CHECK(review_score BETWEEN 1 AND 5), review_comment_title text,
 review_comment_message text, review_creation_date timestamp, review_answer_timestamp timestamp,
 PRIMARY KEY(review_id,order_id));
CREATE INDEX customers_unique_id_idx ON olist.customers(customer_unique_id);
CREATE INDEX orders_customer_id_idx ON olist.orders(customer_id);
CREATE INDEX orders_purchase_timestamp_idx ON olist.orders(order_purchase_timestamp);
CREATE INDEX order_items_product_id_idx ON olist.order_items(product_id);
CREATE INDEX order_items_seller_id_idx ON olist.order_items(seller_id);
CREATE INDEX order_reviews_order_id_idx ON olist.order_reviews(order_id);
CREATE TABLE ds_meta.import_batches (
 batch_id text PRIMARY KEY, started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 completed_at timestamptz NOT NULL DEFAULT clock_timestamp(), dataset_hash text NOT NULL UNIQUE,
 files jsonb NOT NULL, quality jsonb NOT NULL, row_counts jsonb NOT NULL);
CREATE TABLE ds_meta.schema_migrations(version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT clock_timestamp());
INSERT INTO ds_meta.schema_migrations(version) VALUES(1);
RESET ROLE;
