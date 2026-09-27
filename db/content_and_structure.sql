SET VARIABLE artifacts = '/Users/home/workspace/School/Advanced_Software_Engineering/Project/work/SWE-Group-Project--Project-3/src/data/gitskills_data/data/artifacts/*.parquet';

COPY (
WITH s AS (
  SELECT file_sha, location_class, body_chars, frontmatter_valid,
         coalesce(description, '') AS description,
         regexp_replace(content, '^\s*---.*?\n---\s*\n?', '', 's') AS body,   -- text after front matter
         regexp_extract(content, '^\s*---(.*?)\n---', 1, 's')      AS fm      -- front matter block
  FROM read_parquet(getvariable('artifacts'))
  WHERE dedup_primary = 1
    AND NOT (strpos(content, chr(10)) = 0 AND content LIKE '%SKILL.md')
    AND content NOT LIKE 'Redirecting%'
    AND body_chars >= 200
)
SELECT file_sha, location_class, frontmatter_valid, body_chars,
       length(description)                                                    AS desc_chars,
       length(regexp_split_to_array(trim(body), '\s+'))                       AS body_words,
       len(regexp_extract_all(body, '(?m)^#{1,6}\s'))                         AS headings,
       len(regexp_extract_all(body, '(?m)^##\s'))                             AS h2,
       len(regexp_extract_all(body, '(?m)^\s*\d+[.)]\s'))                     AS numbered_items,
       len(regexp_extract_all(body, '(?m)^\s*[-*+]\s'))                       AS bullet_items,
       len(regexp_extract_all(body, '(?m)^\s*```')) // 2                      AS code_blocks,
       len(regexp_extract_all(body, '(?m)^\|.*\|\s*$'))                       AS table_rows,
       len(regexp_extract_all(body, '\[[^\]]+\]\([^)]+\)'))                   AS links,
       regexp_matches(lower(body), '(?m)^#{1,6}\s*(examples?|usage|quick start)') AS has_examples_section,
       regexp_matches(lower(body), '(?m)^#{1,6}\s*(when to use|use when|triggers?)') AS has_when_to_use,
       regexp_matches(lower(fm), '(?m)^allowed-tools\s*:')                    AS fm_allowed_tools,
       regexp_matches(lower(fm), '(?m)^license\s*:')                          AS fm_license,
       regexp_matches(lower(fm), '(?m)^(version|metadata)\s*:')               AS fm_version_or_metadata,
       length(regexp_replace(body, '[\x00-\x7F]', '', 'g')) * 1.0
         / greatest(length(body), 1)                                          AS non_ascii_ratio
FROM s
) TO '/Users/home/workspace/School/Advanced_Software_Engineering/Project/work/SWE-Group-Project--Project-3/src/data/generated_data/skill_features.parquet' (FORMAT parquet, COMPRESSION zstd);

SELECT COUNT(*)                                            AS skills,
       quantile_cont(body_words, [.1, .25, .5, .75, .9])  AS body_words_p10_p25_p50_p75_p90,
       quantile_cont(desc_chars, [.1, .5, .9])            AS desc_chars_p10_p50_p90,
       median(headings)                                    AS median_headings,
       ROUND(AVG((headings = 0)::INT), 3)                  AS share_no_headings,
       ROUND(AVG((numbered_items > 0)::INT), 3)            AS share_numbered_steps,
       ROUND(AVG((bullet_items > 0)::INT), 3)              AS share_bullets,
       ROUND(AVG((code_blocks > 0)::INT), 3)               AS share_code_blocks,
       ROUND(AVG((table_rows > 0)::INT), 3)                AS share_tables,
       ROUND(AVG((links > 0)::INT), 3)                     AS share_links,
       ROUND(AVG(has_examples_section::INT), 3)            AS share_examples_section,
       ROUND(AVG(has_when_to_use::INT), 3)                 AS share_when_to_use,
       ROUND(AVG(fm_allowed_tools::INT), 3)                AS share_fm_allowed_tools,
       ROUND(AVG(fm_license::INT), 3)                      AS share_fm_license,
       ROUND(AVG(fm_version_or_metadata::INT), 3)          AS share_fm_version_metadata,
       ROUND(AVG((non_ascii_ratio > 0.2)::INT), 3)         AS share_mostly_non_english
FROM '/Users/home/workspace/School/Advanced_Software_Engineering/Project/work/SWE-Group-Project--Project-3/src/data/generated_data/skill_features.parquet';

SELECT location_class, COUNT(*) AS skills,
       median(body_words)                          AS median_words,
       median(headings)                            AS median_headings,
       ROUND(AVG((code_blocks > 0)::INT), 3)       AS share_code_blocks,
       ROUND(AVG(has_examples_section::INT), 3)    AS share_examples_section,
       ROUND(AVG(has_when_to_use::INT), 3)         AS share_when_to_use
FROM '/Users/home/workspace/School/Advanced_Software_Engineering/Project/work/SWE-Group-Project--Project-3/src/data/generated_data/skill_features.parquet'
GROUP BY 1 ORDER BY skills DESC;

SELECT CASE WHEN body_words < 100 THEN '<100' WHEN body_words < 250 THEN '100-249'
            WHEN body_words < 500 THEN '250-499' WHEN body_words < 1000 THEN '500-999'
            WHEN body_words < 2000 THEN '1000-1999' WHEN body_words < 5000 THEN '2000-4999'
            ELSE '5000+' END AS words, MIN(body_words) AS sort_key, COUNT(*) AS skills
FROM '/Users/home/workspace/School/Advanced_Software_Engineering/Project/work/SWE-Group-Project--Project-3/src/data/generated_data/skill_features.parquet'
GROUP BY 1 ORDER BY sort_key;