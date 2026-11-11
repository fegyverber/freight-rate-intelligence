-- One row per deal: neither monthly date expansion nor body-group membership
-- is allowed to multiply labels in the ML sample.
WITH body_groups AS (
    SELECT price_id,
           string_agg(body_group, ' | ' ORDER BY body_group_id) AS body_group
    FROM core.price_body_groups
    GROUP BY price_id
)
SELECT p.price_id,
       p.loading_date,
       p.loading_city_gcid,
       p.unloading_city_gcid,
       p.distance,
       p.total_weight,
       p.total_volume,
       p.body_type,
       COALESCE(b.body_group, 'Не указан') AS body_group,
       p.weight_category,
       p.volume_category,
       origin.full_name_local AS origin_full_name,
       destination.full_name_local AS destination_full_name,
       origin.latitude AS origin_latitude,
       origin.longitude AS origin_longitude,
       destination.latitude AS destination_latitude,
       destination.longitude AS destination_longitude,
       p.price_type,
       p.is_fact,
       p.complex,
       p.outlier_mark,
       CASE WHEN p.loading_date <= %(as_of)s
            THEN p.price_rub::double precision
            ELSE NULL END AS actual_price_rub
FROM core.prices p
JOIN core.dim_cities origin ON origin.gc_id = p.loading_city_gcid
JOIN core.dim_cities destination ON destination.gc_id = p.unloading_city_gcid
LEFT JOIN body_groups b ON b.price_id = p.price_id
WHERE p.loading_date >= %(train_start)s
  AND NOT p.complex
ORDER BY p.loading_date, p.price_id;
