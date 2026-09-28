SET VARIABLE repos = '/Users/home/workspace/School/Advanced_Software_Engineering/Project/work/SWE-Group-Project--Project-3/src/data/gitskills_data/data/repos/*.parquet';
SET VARIABLE artifacts = '/Users/home/workspace/School/Advanced_Software_Engineering/Project/work/SWE-Group-Project--Project-3/src/data/gitskills_data/data/artifacts/*.parquet';
SET VARIABLE repo_profile = '/Users/home/workspace/School/Advanced_Software_Engineering/Project/work/SWE-Group-Project--Project-3/src/data/generated_data/repo_profile.parquet';

COPY (
WITH per_repo AS (
  SELECT repo_full_name,
         COUNT(*)                   AS files,          -- all SKILL.md files in the repo
         COUNT(DISTINCT file_sha)   AS distinct_skills,
         COUNT_IF(dedup_primary = 0) AS copy_rows      -- files whose text is represented elsewhere
  FROM read_parquet(getvariable('artifacts'))
  GROUP BY 1
)
SELECT r.full_name, r.owner, r.stars, r.forks, r.is_fork, r.language, r.license,
       r.created_at, r.pushed_at,
       p.files, p.distinct_skills, p.copy_rows,
       ROUND(1.0 * p.copy_rows / p.files, 3) AS copy_share,
       regexp_matches(lower(r.full_name), 'skill')                      AS name_has_skill,
       regexp_matches(lower(r.full_name), 'awesome|collection|library|marketplace|registry|hub') AS name_is_collection,
       regexp_matches(lower(r.full_name), 'claude|agent|codex|cursor|copilot|gemini') AS name_has_agent,
       CASE WHEN p.files = 1 THEN '1' WHEN p.files <= 5 THEN '2-5' WHEN p.files <= 20 THEN '6-20'
            WHEN p.files <= 100 THEN '21-100' WHEN p.files <= 1000 THEN '101-1000' ELSE '>1000' END AS size_tier
FROM read_parquet(getvariable('repos')) r
JOIN per_repo p ON p.repo_full_name = r.full_name
) TO '/Users/home/workspace/School/Advanced_Software_Engineering/Project/work/SWE-Group-Project--Project-3/src/data/generated_data/repo_profile.parquet' (FORMAT parquet);

