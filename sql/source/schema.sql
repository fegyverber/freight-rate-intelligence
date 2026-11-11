-- Эталонная DDL-схема операционной БД deals_bigdata (PostgreSQL 17).
-- Схема синтетического источника для демонстрации ETL, аналитических витрин и ML-пайплайна.

CREATE TABLE migrations (
    id        serial PRIMARY KEY,
    migration varchar(255) NOT NULL,
    batch     integer      NOT NULL
);

-- 2020_08_28_070508_create_cities_table
CREATE TABLE cities (
    id              bigserial PRIMARY KEY,
    full_name_local varchar(255) NOT NULL,
    full_name_gc    varchar(255) NOT NULL,
    gc_id           integer      NOT NULL,
    name            varchar(255) NOT NULL,
    longitude       double precision,
    latitude        double precision,
    created_at      timestamp(0),
    updated_at      timestamp(0)
);
CREATE INDEX cities_gc_id_index ON cities (gc_id);

-- 2020_08_28_080614_create_body_types_table + 2026_01_20_121758 (std_id)
CREATE TABLE body_types (
    id           bigserial PRIMARY KEY,
    name         varchar(255) NOT NULL,
    "id_from_1C" integer      NOT NULL,
    std_id       integer
);

-- 2023_02_09_043115 + 2023_10_19_113242 + 2024_07_01_123949 (value -> double precision)
CREATE TABLE exchange_rates (
    id           bigserial PRIMARY KEY,
    num_code     varchar(255)     NOT NULL,
    value        double precision NOT NULL,
    setting_date date             NOT NULL
);
CREATE INDEX exchange_rates_num_code_idx ON exchange_rates (num_code, setting_date);

-- 2026_01_28_094027_add_price_group_stats_table
CREATE TABLE price_group_stats (
    id                  bigserial PRIMARY KEY,
    created_at          timestamp(0),
    updated_at          timestamp(0),
    loading_month       date        NOT NULL,
    loading_city_gcid   integer     NOT NULL,
    unloading_city_gcid integer     NOT NULL,
    body_type_std_id    integer     NOT NULL,
    weight_category     varchar(4)  NOT NULL,
    volume_category     varchar(4)  NOT NULL,
    min_km_price        double precision,
    max_km_price        double precision,
    outliers_amount     integer,
    clear_items_amount  integer,
    avg_price           double precision
);

-- 2023_09_21_055520_create_prices_table и последующие alter:
-- meta_item_id, end_loading_date, nearest_rate_date::date, drop deleted_at/*_city_full/body_types,
-- source, external_item_uuid, outlier_mark, weight/volume_category, price_group_id
CREATE TABLE prices (
    id                  bigserial PRIMARY KEY,
    created_at          timestamp(0),
    updated_at          timestamp(0),
    subscriber_id       integer,
    organization_id     integer,
    loading_date        date             NOT NULL,
    loading_city_gcid   integer          NOT NULL,
    unloading_city_gcid integer          NOT NULL,
    distance            double precision NOT NULL,
    total_weight        double precision NOT NULL,
    total_volume        double precision NOT NULL,
    body_type           integer,
    price               double precision NOT NULL,
    currency_code       varchar(3)       NOT NULL DEFAULT '643',
    complex             boolean          NOT NULL DEFAULT false,
    price_type          smallint         NOT NULL DEFAULT 2,
    item_id             integer,
    is_fact             boolean          NOT NULL DEFAULT true,
    nearest_rate_date   date,
    meta_item_id        bigint,
    end_loading_date    date,
    source              varchar(255),
    external_item_uuid  uuid,
    outlier_mark        boolean          NOT NULL DEFAULT false,
    weight_category     varchar(4),
    volume_category     varchar(4),
    price_group_id      bigint REFERENCES price_group_stats (id)
);
CREATE INDEX prices_subscriber_id_index ON prices (subscriber_id);
CREATE INDEX prices_meta_item_id_index ON prices (meta_item_id);
CREATE INDEX main_prices_index ON prices (loading_city_gcid, unloading_city_gcid, total_weight, total_volume);
CREATE INDEX main_prices_cat_index ON prices (loading_city_gcid, unloading_city_gcid, weight_category, volume_category, outlier_mark);
CREATE INDEX price_group_index ON prices (price_group_id);

-- 2023_10_13_051908_create_price_body_types_table
CREATE TABLE price_body_types (
    id           bigserial PRIMARY KEY,
    price_id     bigint  NOT NULL REFERENCES prices (id) ON DELETE CASCADE,
    body_type_id integer NOT NULL,
    UNIQUE (price_id, body_type_id)
);

-- 2023_10_20_080541_create_price_dates_table (индекса на price_date нет)
CREATE TABLE price_dates (
    id         bigserial PRIMARY KEY,
    price_id   bigint NOT NULL REFERENCES prices (id) ON DELETE CASCADE,
    price_date date   NOT NULL,
    UNIQUE (price_id, price_date)
);

-- 2024_12_23_133222_create_price_participants_table
CREATE TABLE price_participants (
    id            bigserial PRIMARY KEY,
    price_id      bigint NOT NULL REFERENCES prices (id) ON DELETE CASCADE,
    subscriber_id bigint NOT NULL
);

-- 2024_11_26_171239_create_selected_directions_table
CREATE TABLE selected_directions (
    id                   bigserial PRIMARY KEY,
    subscriber_id        bigint NOT NULL,
    loading_city_gc_id   bigint NOT NULL,
    unloading_city_gc_id bigint NOT NULL,
    body_type_id         bigint NOT NULL REFERENCES body_types (id) ON DELETE CASCADE,
    created_at           timestamp(0),
    updated_at           timestamp(0)
);

-- 2024_11_27_151234_create_favorite_directions_table
CREATE TABLE favorite_directions (
    id                   bigserial PRIMARY KEY,
    subscriber_id        bigint NOT NULL,
    loading_city_gc_id   bigint NOT NULL,
    unloading_city_gc_id bigint NOT NULL,
    body_type_id         bigint NOT NULL,
    created_at           timestamp(0),
    updated_at           timestamp(0)
);
