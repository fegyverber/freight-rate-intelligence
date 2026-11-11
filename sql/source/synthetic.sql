-- Генератор синтетических логистических данных для локального pet-проекта (PostgreSQL 17, схема sql/schema.sql).
-- Данные детерминированы (setseed) и воспроизводят реальные краевые случаи предметной области:
--   * дубли cities.gc_id (одинаковое и разное full_name_local);
--   * цены с несколькими типами кузова (в т.ч. в разных группах) и без price_body_types;
--   * дочерние id кузова (57, 46, 1) в price_body_types у старых записей;
--   * многомесячные периоды (end_loading_date) и даты погрузки после as_of;
--   * валюта 840/978 и записи без курса (nearest_rate_date IS NULL);
--   * outlier_mark, complex, price_type 1/2/3/4/5/6/9, цепочки участников (price_participants);
--   * границы весовых категорий (10.0, 30.0 т) и объёма (0, 120 м³);
--   * выбранные и избранные направления, голоса за сентябрь и октябрь 2026.
-- Дата снимка: 2026-10-26, последние записи созданы в этот день.
-- Объём задаётся переменной psql: psql -v n_prices=3000000 -f synthetic.sql (по умолчанию 300 000 записей prices).

\if :{?n_prices}
\else
\set n_prices 300000
\endif

SELECT setseed(0.20261026);

-- body_types: как database/seeders/FillingBodyTypes.php (id = id_from_1C), фрагмент с группами и прочими
INSERT INTO body_types (id, name, "id_from_1C", std_id) VALUES
 (1,'бортовая',1,15),(2,'изотермический',2,2),(3,'контейнер',3,3),(4,'рефрижератор',4,4),(5,'трал',5,5),
 (15,'борт',15,15),(26,'контейнерная площадка',26,3),(46,'рефрижератор мультирежимный',46,4),
 (47,'рефрижератор с перегородкой',47,4),(48,'рефрижератор-тушевоз',48,4),(50,'самосвал',50,50),
 (56,'тентованный',56,56),(57,'тент бок',57,56),(58,'тент верх',58,56),(59,'тент зад и бок',59,56),
 (60,'тент задняя загрузка',60,56),(61,'тент полная растентовка',61,56),(65,'фургон',65,65),(68,'цистерна',68,68);

-- cities: 40 городов; у Москвы и Казани по две строки с одним gc_id (одинаковое имя / разное имя)
INSERT INTO cities (full_name_local, full_name_gc, gc_id, name, latitude, longitude, created_at, updated_at)
SELECT c.full_name, c.full_name, c.gc_id, split_part(c.full_name, ',', 1), c.lat, c.lon, now(), now()
FROM (VALUES
 (1001,'Москва, г. Москва',55.7558,37.6173),(1002,'Санкт-Петербург, г. Санкт-Петербург',59.9343,30.3351),
 (1003,'Казань, Республика Татарстан',55.7961,49.1064),(1004,'Екатеринбург, Свердловская обл.',56.8389,60.6057),
 (1005,'Новосибирск, Новосибирская обл.',55.0084,82.9357),(1006,'Нижний Новгород, Нижегородская обл.',56.2965,43.9361),
 (1007,'Самара, Самарская обл.',53.1959,50.1002),(1008,'Ростов-на-Дону, Ростовская обл.',47.2357,39.7015),
 (1009,'Краснодар, Краснодарский край',45.0355,38.9753),(1010,'Воронеж, Воронежская обл.',51.6720,39.1843),
 (1011,'Челябинск, Челябинская обл.',55.1644,61.4368),(1012,'Уфа, Республика Башкортостан',54.7388,55.9721),
 (1013,'Пермь, Пермский край',58.0105,56.2502),(1014,'Волгоград, Волгоградская обл.',48.7080,44.5133),
 (1015,'Красноярск, Красноярский край',56.0153,92.8932),(1016,'Тюмень, Тюменская обл.',57.1530,65.5343),
 (1017,'Саратов, Саратовская обл.',51.5331,46.0342),(1018,'Тольятти, Самарская обл.',53.5303,49.3461),
 (1019,'Ижевск, Удмуртская Республика',56.8526,53.2045),(1020,'Барнаул, Алтайский край',53.3548,83.7698),
 (1021,'Ульяновск, Ульяновская обл.',54.3142,48.4031),(1022,'Иркутск, Иркутская обл.',52.2870,104.3050),
 (1023,'Хабаровск, Хабаровский край',48.4802,135.0719),(1024,'Ярославль, Ярославская обл.',57.6261,39.8845),
 (1025,'Владивосток, Приморский край',43.1155,131.8855),(1026,'Махачкала, Республика Дагестан',42.9849,47.5047),
 (1027,'Томск, Томская обл.',56.4847,84.9482),(1028,'Оренбург, Оренбургская обл.',51.7682,55.0969),
 (1029,'Кемерово, Кемеровская обл.',55.3547,86.0873),(1030,'Рязань, Рязанская обл.',54.6292,39.7364),
 (1031,'Набережные Челны, Республика Татарстан',55.7436,52.3958),(1032,'Пенза, Пензенская обл.',53.1959,45.0183),
 (1033,'Липецк, Липецкая обл.',52.6088,39.5992),(1034,'Тула, Тульская обл.',54.1931,37.6173),
 (1035,'Киров, Кировская обл.',58.6036,49.6680),(1036,'Калининград, Калининградская обл.',54.7104,20.4522),
 (1037,'Белгород, Белгородская обл.',50.5997,36.5983),(1038,'Курск, Курская обл.',51.7304,36.1926),
 (1039,'Тверь, Тверская обл.',56.8587,35.9176),(1040,'Смоленск, Смоленская обл.',54.7826,32.0453)
) AS c(gc_id, full_name, lat, lon);
INSERT INTO cities (full_name_local, full_name_gc, gc_id, name, latitude, longitude, created_at, updated_at) VALUES
 ('Москва, г. Москва', 'Москва, г. Москва', 1001, 'Москва', 55.7558, 37.6173, now(), now()),
 ('Казань г, Татарстан', 'Казань г, Татарстан', 1003, 'Казань', 55.7961, 49.1064, now(), now());

-- exchange_rates: курсы ЦБ (вт–сб), 643 = 1 на каждую дату
INSERT INTO exchange_rates (num_code, value, setting_date)
SELECT x.code, x.v, d::date
FROM generate_series(DATE '2023-06-01', DATE '2026-10-26', INTERVAL '1 day') AS d
CROSS JOIN LATERAL (VALUES
  ('643', 1.0::float8),
  ('840', round((88 + 10 * sin(extract(epoch FROM d) / 8e6))::numeric, 4)::float8),
  ('978', round((95 + 11 * sin(extract(epoch FROM d) / 8e6))::numeric, 4)::float8)
) AS x(code, v)
WHERE extract(isodow FROM d) NOT IN (1, 7);

-- prices: :n_prices записей (по умолчанию 300 000)
CREATE TEMP TABLE gen AS
WITH base AS (
  SELECT g AS n,
         random() AS r_route1, random() AS r_route2, random() AS r_date, random() AS r_type,
         random() AS r_w, random() AS r_w2, random() AS r_v, random() AS r_bt, random() AS r_cur,
         random() AS r_noise1, random() AS r_noise2, random() AS r_out, random() AS r_cx,
         random() AS r_end, random() AS r_sub, random() AS r_fut, random() AS r_created
  FROM generate_series(1, :n_prices) AS g
)
SELECT b.*,
       -- популярность маршрутов по Ципфу: первые города чаще
       1001 + least(39, floor(40 * power(b.r_route1, 2.2)))::int AS lc,
       1001 + least(39, floor(40 * power(b.r_route2, 1.6)))::int AS uc0,
       CASE
         WHEN b.r_fut < 0.02 THEN DATE '2026-10-27' + floor(b.r_date * 110)::int          -- плановые погрузки после as_of
         ELSE DATE '2026-10-26' - floor(power(b.r_date, 1.35) * 1120)::int                -- история ~37 мес., больше свежих
       END AS ld
FROM base b;

INSERT INTO prices (id, created_at, updated_at, subscriber_id, organization_id, loading_date, loading_city_gcid,
                    unloading_city_gcid, distance, total_weight, total_volume, body_type, price, currency_code,
                    complex, price_type, item_id, is_fact, nearest_rate_date, meta_item_id, end_loading_date,
                    source, outlier_mark, weight_category, volume_category)
WITH g1 AS (
  SELECT g.*,
         CASE WHEN g.uc0 = g.lc THEN 1001 + ((g.lc - 1001 + 7) % 40) ELSE g.uc0 END AS uc,
         CASE
           WHEN g.ld < DATE '2024-06-01' AND g.r_type < 0.03 THEN 4
           WHEN g.ld < DATE '2024-06-01' AND g.r_type < 0.05 THEN 5
           WHEN g.r_type < 0.27 THEN 1
           WHEN g.r_type < 0.55 THEN 2
           WHEN g.r_type < 0.70 THEN 3
           WHEN g.r_type < 0.86 THEN 6
           ELSE 9
         END AS pt,
         CASE
           WHEN g.r_w < 0.05 THEN 30 + round((g.r_w2 * 8)::numeric, 1)::float8                 -- G20T
           WHEN g.r_w < 0.08 THEN CASE WHEN g.r_w2 < 0.5 THEN 10.0 ELSE 30.0 END              -- границы
           WHEN g.r_w < 0.60 THEN 10 + round((g.r_w2 * 19.9)::numeric, 1)::float8              -- FTL
           WHEN g.r_w < 0.72 THEN 5 + round((g.r_w2 * 4.9)::numeric, 1)::float8                -- 10T
           WHEN g.r_w < 0.82 THEN 2.5 + round((g.r_w2 * 2.4)::numeric, 1)::float8              -- 5T
           WHEN g.r_w < 0.94 THEN 0.5 + round((g.r_w2 * 1.9)::numeric, 1)::float8              -- L5T
           ELSE round((g.r_w2 * 0.4)::numeric, 2)::float8                                     -- OTH
         END AS w,
         CASE
           WHEN g.r_v < 0.08 THEN 0::float8
           WHEN g.r_v < 0.12 THEN 120::float8
           WHEN g.r_v < 0.16 THEN 130 + round((g.r_v * 40)::numeric)::float8
           ELSE 10 + round((g.r_v * 100)::numeric)::float8
         END AS v,
         CASE
           WHEN g.r_bt < 0.55 THEN 56 WHEN g.r_bt < 0.75 THEN 4 WHEN g.r_bt < 0.82 THEN 2
           WHEN g.r_bt < 0.90 THEN 15 WHEN g.r_bt < 0.93 THEN 3 WHEN g.r_bt < 0.96 THEN 50
           WHEN g.r_bt < 0.98 THEN 65 ELSE 68
         END AS bt_std
  FROM gen g
), g2 AS (
  SELECT g1.*,
         c1.latitude AS la1, c1.longitude AS lo1, c2.latitude AS la2, c2.longitude AS lo2,
         CASE
           WHEN g1.r_end < 0.05 THEN g1.ld + (30 + floor(g1.r_end * 1800))::int               -- многомесячные периоды
           ELSE g1.ld
         END AS eld,
         least(TIMESTAMP '2026-10-26 22:40:00',
               g1.ld - make_interval(days => floor(g1.r_created * 12)::int) + make_interval(mins => floor(g1.r_created * 1440)::int)) AS cr,
         -- абоненты: 1..3000, 7 крупных абонентов дают ~15% записей
         CASE WHEN g1.r_sub < 0.15 THEN 101 + floor(g1.r_sub * 1000 / 21.5)::int
              ELSE 101 + floor(g1.r_sub * 3000)::int END AS sub
  FROM g1
  JOIN LATERAL (SELECT latitude, longitude FROM cities WHERE gc_id = g1.lc ORDER BY id LIMIT 1) c1 ON true
  JOIN LATERAL (SELECT latitude, longitude FROM cities WHERE gc_id = g1.uc ORDER BY id LIMIT 1) c2 ON true
), g3 AS (
  SELECT g2.*,
         round((1.25 * 6371 * acos(least(1, cos(radians(la1)) * cos(radians(la2)) * cos(radians(lo2) - radians(lo1))
                 + sin(radians(la1)) * sin(radians(la2)))))::numeric)::float8 AS dist,
         -- сезонность: пик в декабре и июне-июле, рост ~12% в год
         (1 + 0.08 * cos(2 * pi() * (extract(month FROM g2.ld) - 12) / 12) + 0.04 * cos(2 * pi() * (extract(month FROM g2.ld) - 7) / 6))
           * power(1.12, (g2.ld - DATE '2023-10-01') / 365.0) AS season_trend
  FROM g2
)
SELECT g3.n,
       g3.cr, g3.cr,
       g3.sub,
       100000 + (g3.sub * 7) % 2500,
       g3.ld, g3.lc, g3.uc, g3.dist, g3.w, g3.v,
       CASE WHEN g3.r_bt BETWEEN 0.4 AND 0.42 THEN NULL ELSE g3.bt_std END,
       CASE
         WHEN g3.r_noise1 < 0.0005 THEN 0
         ELSE round(((CASE g3.bt_std WHEN 56 THEN 72 WHEN 4 THEN 95 WHEN 2 THEN 84 WHEN 15 THEN 68 ELSE 80 END * g3.dist + 6000)
           * g3.season_trend
           * CASE WHEN g3.w >= 30 THEN 1.1 WHEN g3.w >= 10 THEN 1 WHEN g3.w >= 5 THEN 0.75 WHEN g3.w >= 2.5 THEN 0.55
                  WHEN g3.w >= 0.5 THEN 0.4 ELSE 0.35 END
           * CASE g3.pt WHEN 6 THEN 0.93 WHEN 3 THEN 0.9 WHEN 9 THEN 0.97 ELSE 1 END
           * exp(0.16 * sqrt(-2 * ln(greatest(g3.r_noise1, 1e-9))) * cos(2 * pi() * g3.r_noise2))
           * CASE WHEN g3.r_out < 0.012 THEN 0.25 WHEN g3.r_out > 0.988 THEN 4 ELSE 1 END
           / CASE WHEN g3.r_cur < 0.02 THEN 90 WHEN g3.r_cur < 0.03 THEN 100 ELSE 1 END)::numeric, 2)::float8
       END,
       CASE WHEN g3.r_cur < 0.02 THEN '840' WHEN g3.r_cur < 0.03 THEN '978' ELSE '643' END,
       g3.r_cx < 0.03,
       g3.pt,
       5000000 + g3.n,
       g3.pt IN (1, 2, 3, 4, 5, 9),
       CASE WHEN g3.r_cx > 0.995 THEN NULL
            ELSE (SELECT max(setting_date) FROM exchange_rates er WHERE er.num_code = '643' AND er.setting_date <= g3.cr) END,
       CASE WHEN g3.pt = 6 THEN 900000 + g3.n / 3 END,
       g3.eld,
       CASE WHEN g3.pt IN (1, 2, 3) THEN 'data-hub' WHEN g3.pt = 6 THEN 'shippers-api' WHEN g3.pt = 9 THEN 'sbt' END,
       -- выбросы помечены ночным прогоном; записи от 26.10.2026 ещё не размечены
       g3.pt IN (1, 2, 3, 4, 5, 9) AND (g3.r_out < 0.010 OR g3.r_out > 0.990) AND g3.cr < TIMESTAMP '2026-10-26 02:30:00',
       CASE WHEN round(g3.w::numeric, 1) >= 30 THEN 'G20T' WHEN round(g3.w::numeric, 1) >= 10 THEN 'FTL'
            WHEN round(g3.w::numeric, 1) >= 5 THEN '10T' WHEN round(g3.w::numeric, 1) >= 2.5 THEN '5T'
            WHEN round(g3.w::numeric, 1) >= 0.5 THEN 'L5T' ELSE 'OTH' END,
       CASE WHEN g3.v > 0 AND g3.v <= 120 THEN 'FTL' ELSE 'OTH' END
FROM g3;
SELECT setval('prices_id_seq', (SELECT max(id) FROM prices));

-- price_body_types: основной тип; 12% — второй тип (тент+борт, реф+изотерм), 2% — дочерние id у старых записей,
-- 2% — без строк вовсе
INSERT INTO price_body_types (price_id, body_type_id)
SELECT p.id,
       CASE WHEN p.loading_date < DATE '2025-01-01' AND p.id % 50 = 0
            THEN CASE p.body_type WHEN 56 THEN 57 WHEN 4 THEN 46 WHEN 15 THEN 1 ELSE p.body_type END
            ELSE p.body_type END
FROM prices p
WHERE p.body_type IS NOT NULL AND p.id % 50 <> 7;
INSERT INTO price_body_types (price_id, body_type_id)
SELECT p.id, CASE p.body_type WHEN 56 THEN 15 WHEN 15 THEN 56 WHEN 4 THEN 2 WHEN 2 THEN 4 ELSE 56 END
FROM prices p
WHERE p.body_type IS NOT NULL AND p.id % 50 <> 7 AND p.id % 8 = 3
ON CONFLICT DO NOTHING;
INSERT INTO price_body_types (price_id, body_type_id)
SELECT p.id, 61 FROM prices p WHERE p.body_type = 56 AND p.id % 97 = 5 ON CONFLICT DO NOTHING;

-- price_dates: как PriceService::createPriceDates — месяцы от loading_date до end_loading_date
INSERT INTO price_dates (price_id, price_date)
SELECT p.id, least((greatest(p.loading_date, DATE '2019-01-01') + make_interval(months => k))::date, p.end_loading_date)
FROM prices p
CROSS JOIN LATERAL generate_series(0,
   (extract(year FROM age(date_trunc('month', p.end_loading_date), date_trunc('month', greatest(p.loading_date, DATE '2019-01-01')))) * 12
  + extract(month FROM age(date_trunc('month', p.end_loading_date), date_trunc('month', greatest(p.loading_date, DATE '2019-01-01')))))::int) AS k;

-- price_participants: владелец записи + участники цепочки general_deal_id у 10% заявок 1/2
INSERT INTO price_participants (price_id, subscriber_id)
SELECT id, subscriber_id FROM prices WHERE subscriber_id IS NOT NULL;
INSERT INTO price_participants (price_id, subscriber_id)
SELECT id, 101 + (subscriber_id * 13 + 5) % 3000 FROM prices WHERE price_type IN (1, 2) AND id % 10 = 1;
-- крупный абонент 101 участвует в цепочках ещё на 3% заявок
INSERT INTO price_participants (price_id, subscriber_id)
SELECT id, 101 FROM prices WHERE price_type IN (1, 2) AND id % 33 = 2 AND subscriber_id <> 101;

-- price_group_stats и prices.price_group_id: месяц loading_date × маршрут × кузов × вес × объём
INSERT INTO price_group_stats (created_at, updated_at, loading_month, loading_city_gcid, unloading_city_gcid,
                               body_type_std_id, weight_category, volume_category, min_km_price, max_km_price,
                               outliers_amount, clear_items_amount, avg_price)
SELECT now(), now(), date_trunc('month', loading_date)::date, loading_city_gcid, unloading_city_gcid, body_type,
       weight_category, volume_category,
       percentile_cont(0.05) WITHIN GROUP (ORDER BY price / nullif(distance, 0)),
       percentile_cont(0.95) WITHIN GROUP (ORDER BY price / nullif(distance, 0)),
       count(*) FILTER (WHERE outlier_mark), count(*) FILTER (WHERE NOT outlier_mark), avg(price)
FROM prices
WHERE body_type IS NOT NULL AND is_fact
GROUP BY 3, 4, 5, 6, 7, 8;
UPDATE prices p SET price_group_id = s.id
FROM price_group_stats s
WHERE s.loading_month = date_trunc('month', p.loading_date)::date AND s.loading_city_gcid = p.loading_city_gcid
  AND s.unloading_city_gcid = p.unloading_city_gcid AND s.body_type_std_id = p.body_type
  AND s.weight_category = p.weight_category AND s.volume_category = p.volume_category;

-- selected_directions: 40 абонентов, до 10 направлений на группу (56, 4, 15)
INSERT INTO selected_directions (subscriber_id, loading_city_gc_id, unloading_city_gc_id, body_type_id, created_at, updated_at)
SELECT s.sub, r.lc, r.uc, r.bt, TIMESTAMP '2025-03-01' + make_interval(days => (s.sub * 7 + r.k) % 500), now()
FROM generate_series(101, 140) AS s(sub)
CROSS JOIN LATERAL (
  SELECT k, 1001 + (s.sub * 3 + k) % 12 AS lc, 1001 + (s.sub * 5 + k * 7 + 1) % 20 AS uc,
         (ARRAY[56, 4, 15])[1 + (k % 3)] AS bt
  FROM generate_series(1, 4 + s.sub % 20) AS k
) r
WHERE r.lc <> r.uc;
-- голоса за маршруты: сентябрь 2026 (используется отчётом favorite) и октябрь 2026
INSERT INTO favorite_directions (subscriber_id, loading_city_gc_id, unloading_city_gc_id, body_type_id, created_at, updated_at)
SELECT DISTINCT ON (v.sub, v.lc, v.uc, v.bt, v.m) v.sub, v.lc, v.uc, v.bt, v.m + make_interval(days => v.sub % 25), now()
FROM (
  SELECT s.sub, 1001 + least(39, floor(40 * power(random(), 2.2)))::int AS lc,
         1001 + least(39, floor(40 * power(random(), 1.6)))::int AS uc,
         (ARRAY[56, 56, 56, 4, 15])[1 + floor(random() * 5)::int] AS bt,
         (ARRAY[TIMESTAMP '2026-09-01', TIMESTAMP '2026-10-01'])[1 + floor(random() * 2)::int] AS m
  FROM generate_series(101, 400) AS s(sub), generate_series(1, 6)
) v
WHERE v.lc <> v.uc;

ANALYZE;
