-- Which repositories count as registries (skill sources)?
-- Run views.sql first.
--
-- In plain words:
--   1. Keep only the skill texts that are in English.
--   2. For each repository, count how many different English skills it has.
--   3. Keep a repository when it has at least 100 stars, is not a fork,
--      and has at least 5 different English skills.
--
-- Result on the full data: 3,566 registries.

WITH english_skills AS (
    SELECT file_sha
    FROM skill_language
    WHERE is_english
),

skills_per_repo AS (
    SELECT artifacts.repo_full_name           AS repo,
           count(DISTINCT artifacts.file_sha) AS english_skills,
           count(*)                           AS english_files
    FROM artifacts
    JOIN english_skills ON artifacts.file_sha = english_skills.file_sha
    GROUP BY artifacts.repo_full_name
)

SELECT repos.full_name AS repo,
       repos.owner,
       repos.stars,
       skills_per_repo.english_skills,
       skills_per_repo.english_files
FROM repos
JOIN skills_per_repo ON skills_per_repo.repo = repos.full_name
WHERE repos.stars >= 100
  AND repos.is_fork = 0
  AND skills_per_repo.english_skills >= 5
ORDER BY repos.stars DESC;
