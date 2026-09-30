SET VARIABLE repos = 'src/data/gitskills_data/data/repos/*.parquet';
SET VARIABLE artifacts = 'src/data/gitskills_data/data/artifacts/*.parquet';
SET VARIABLE artifact_siblings = 'src/data/gitskills_data/data/artifact_siblings/*.parquet';
SET VARIABLE mining_runs = 'src/data/gitskills_data/data/mining_runs/*.parquet';

SELECT SUM(history_fetched = 1) AS with_history,
       COUNT(*)                 AS distinct_skills,
       ROUND(100.0 * SUM(history_fetched = 1) / COUNT(*), 1) AS pct
FROM read_parquet(getvariable('artifacts')) WHERE dedup_primary = 1;

SELECT first_commit_author_type, last_commit_author_type, COUNT(*) AS skills
FROM read_parquet(getvariable('artifacts')) WHERE dedup_primary = 1 AND history_fetched = 1
GROUP BY 1, 2 ORDER BY skills DESC;


SELECT COUNT(*)
FROM read_parquet(getvariable('artifacts'))
WHERE dedup_primary = 1 AND history_fetched = 1
  AND (first_commit_author_type IS NULL OR last_commit_author_type IS NULL);

SELECT SUM(skills) FROM (
    SELECT first_commit_author_type, last_commit_author_type, COUNT(*) AS skills
    FROM read_parquet(getvariable('artifacts'))
    WHERE dedup_primary = 1 AND history_fetched = 1
    GROUP BY 1, 2
);

SELECT first_commit_author_type, last_commit_author_type,
       typeof(first_commit_author_type), COUNT(*)
FROM read_parquet(getvariable('artifacts'))
WHERE dedup_primary = 1 AND history_fetched = 1
  AND first_commit_author_type NOT IN ('User', 'Bot', 'Organization', '')
   OR (dedup_primary = 1 AND history_fetched = 1
       AND last_commit_author_type NOT IN ('User', 'Bot', 'Organization', ''))
GROUP BY 1, 2, 3;

SELECT first_commit_author, COUNT(*) AS skills
FROM read_parquet(getvariable('artifacts')) WHERE dedup_primary = 1 AND history_fetched = 1
  AND first_commit_author_type = 'Bot'
GROUP BY 1 ORDER BY skills DESC;

SELECT CASE
    WHEN lower(first_commit_message) LIKE '%co-authored-by:%claude%'
    OR lower(first_commit_message) LIKE '%co-authored-by:%anthropic%' THEN 'Claude'
    WHEN lower(first_commit_message) LIKE '%co-authored-by:%cursor%'    THEN 'Cursor'
    WHEN lower(first_commit_message) LIKE '%co-authored-by:%copilot%'   THEN 'Copilot'
    WHEN lower(first_commit_message) LIKE '%co-authored-by:%codex%'
    OR lower(first_commit_message) LIKE '%co-authored-by:%openai%'    THEN 'Codex/OpenAI'
    WHEN lower(first_commit_message) LIKE '%co-authored-by:%gemini%'    THEN 'Gemini'
    WHEN lower(first_commit_message) LIKE '%co-authored-by:%'           THEN 'Other co-author'
    ELSE 'No trailer'
    END AS trailer,
COUNT(*) AS skills,
ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct
FROM read_parquet(getvariable('artifacts')) WHERE dedup_primary = 1 AND history_fetched = 1
GROUP BY trailer ORDER BY skills DESC;


SELECT trim(substr(first_commit_message,
                   instr(first_commit_message, 'Co-Authored-By: ') + 16,
                   instr(substr(first_commit_message, instr(first_commit_message, 'Co-Authored-By: ') + 16), '<') - 1)) AS model,
       COUNT(*) AS skills
FROM read_parquet(getvariable('artifacts')) WHERE dedup_primary = 1 AND history_fetched = 1
  AND first_commit_message LIKE '%Co-Authored-By: Claude%'
GROUP BY model ORDER BY skills DESC;

SELECT CASE WHEN commit_count = 1 THEN '1' WHEN commit_count <= 3 THEN '2-3'
            WHEN commit_count <= 10 THEN '4-10' ELSE '>10' END AS commits,
       COUNT(*) AS skills
FROM read_parquet(getvariable('artifacts')) WHERE dedup_primary = 1 AND history_fetched = 1
GROUP BY commits ORDER BY MIN(commit_count);


SELECT repo_full_name, first_commit_at, COUNT(*) AS n
      FROM read_parquet(getvariable('artifacts'))
      WHERE dedup_primary = 1 AND history_fetched = 1
      GROUP BY repo_full_name, first_commit_at
      HAVING n > 1


WITH h AS (
  SELECT a.first_commit_at, a.location_class, r.stars,
         CAST(
           lower(a.first_commit_message) LIKE '%co-authored-by:%claude%'
        OR lower(a.first_commit_message) LIKE '%co-authored-by:%anthropic%'
        OR lower(a.first_commit_message) LIKE '%co-authored-by:%cursor%'
        OR lower(a.first_commit_message) LIKE '%co-authored-by:%copilot%'
        OR lower(a.first_commit_message) LIKE '%co-authored-by:%codex%'
         AS INTEGER) AS ai
  FROM read_parquet(getvariable('artifacts')) a
  LEFT JOIN read_parquet(getvariable('repos')) r ON r.full_name = a.repo_full_name
  WHERE a.dedup_primary = 1 AND a.history_fetched = 1
)
SELECT 'month' AS dim, substr(first_commit_at, 1, 7) AS value,
       COUNT(*) AS skills, ROUND(100.0 * AVG(ai), 1) AS pct_ai
FROM h GROUP BY 2
UNION ALL
SELECT 'location', location_class, COUNT(*), ROUND(100.0 * AVG(ai), 1)
FROM h GROUP BY 2
UNION ALL
SELECT 'stars',
       CASE WHEN stars = 0 THEN '0' WHEN stars < 10 THEN '1-9'
            WHEN stars < 100 THEN '10-99' ELSE '100+' END,
       COUNT(*), ROUND(100.0 * AVG(ai), 1)
FROM h GROUP BY 2
ORDER BY 1, 2;

SELECT (lower(first_commit_message) LIKE '%co-authored-by:%claude%'
     OR lower(first_commit_message) LIKE '%co-authored-by:%cursor%'
     OR lower(first_commit_message) LIKE '%co-authored-by:%copilot%'
     OR lower(first_commit_message) LIKE '%co-authored-by:%codex%') AS ai_trailer,
       COUNT(*)                       AS skills,
       CAST(AVG(body_chars) AS INT)   AS avg_body_chars,
       ROUND(AVG(has_scripts), 3)     AS share_with_scripts,
       ROUND(AVG(frontmatter_valid), 3) AS share_valid_fm,
       ROUND(AVG(commit_count), 2)    AS avg_commits
FROM read_parquet(getvariable('artifacts')) WHERE dedup_primary = 1 AND history_fetched = 1
GROUP BY ai_trailer;


SELECT COUNT(*)                                                    AS distinct_skills,
       COUNT_IF(content IS NULL OR content = '')                   AS no_text,
       COUNT_IF(strpos(content, chr(10)) = 0
                AND content LIKE '%SKILL.md')                      AS symlink_stubs,
       COUNT_IF(content LIKE 'Redirecting%')                       AS redirects,
       COUNT_IF(NOT frontmatter_valid)                             AS invalid_frontmatter,
       COUNT_IF(body_chars < 200)                                  AS short_body,
       COUNT_IF(first_commit_at < '2025-10-01')                    AS pre_format_history,
       COUNT_IF(content IS NOT NULL AND content <> ''
                AND NOT (strpos(content, chr(10)) = 0 AND content LIKE '%SKILL.md')
                AND content NOT LIKE 'Redirecting%'
                AND frontmatter_valid
                AND body_chars >= 200)                             AS kept
FROM read_parquet(getvariable('artifacts'))
WHERE dedup_primary = 1;