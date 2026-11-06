-- Language labels: the numbers in docs/skill_genealogy/scope_walkthrough/stage_02_select_registries/README.md (step 1).
-- Run views.sql first. Each query below stands alone; run them one at a time.
--
-- A "text" is one distinct skill content (one file_sha). The same text saved in
-- many repos is one text but many files.
-- English means the label is 'en' or 'unknown' (too short or unclear to tell).


-- 1. How many distinct skill texts were labelled?
--    Expected: 1,877,981
SELECT count(*) AS texts
FROM skill_language;


-- 2. How many texts are English and how many are not, and what share of all texts is that?
--    Expected: English 1,633,285 (87.0%), not English 244,696 (13.0%)
SELECT is_english,
       count(*)                                         AS texts,
       round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS percent
FROM skill_language
GROUP BY is_english
ORDER BY is_english DESC;


-- 3. Texts per language label, biggest first, with the typical text length.
--    'unknown' = fewer than 20 letters or the detector was unsure (counts as English).
--    'other'   = an alphabet the detector does not cover, such as Greek or Hebrew.
--    Expected: en 1,624,923 · zh 105,142 · ja 30,331 · de 29,634 · pt 18,889 · ko 18,226
--              · es 15,465 · unknown 8,362 (median 19 characters) · other 234
SELECT language,
       is_english,
       count(*)          AS texts,
       median(body_chars) AS median_body_chars
FROM skill_language
GROUP BY language, is_english
ORDER BY texts DESC;


-- 4. How many repositories hold English texts, non-English texts, or both?
--    Expected: English 251,416 · non-English 45,999 · both 15,215
WITH repo_sides AS (
    SELECT artifacts.repo_full_name    AS repo,
           bool_or(skill_language.is_english)     AS has_english,
           bool_or(NOT skill_language.is_english) AS has_non_english
    FROM artifacts
    JOIN skill_language ON artifacts.file_sha = skill_language.file_sha
    GROUP BY artifacts.repo_full_name
)
SELECT count(*) FILTER (WHERE has_english)                     AS repos_with_english,
       count(*) FILTER (WHERE has_non_english)                 AS repos_with_non_english,
       count(*) FILTER (WHERE has_english AND has_non_english) AS repos_with_both
FROM repo_sides;


-- 5. How many registries would pass without the English filter, and how many with it?
--    Same rule both times (100+ stars, not a fork, 5+ different skills); only the skill count differs.
--    Expected: all languages 3,968 · English only 3,566 · lost 402
WITH skills_per_repo AS (
    SELECT artifacts.repo_full_name                                          AS repo,
           count(DISTINCT artifacts.file_sha)                                AS all_skills,
           count(DISTINCT artifacts.file_sha) FILTER (WHERE skill_language.is_english) AS english_skills
    FROM artifacts
    JOIN skill_language ON artifacts.file_sha = skill_language.file_sha
    GROUP BY artifacts.repo_full_name
),
big_repos AS (
    SELECT skills_per_repo.*
    FROM skills_per_repo
    JOIN repos ON repos.full_name = skills_per_repo.repo
    WHERE repos.stars >= 100
      AND repos.is_fork = 0
)
SELECT count(*) FILTER (WHERE all_skills >= 5)     AS registries_all_languages,
       count(*) FILTER (WHERE english_skills >= 5) AS registries_english_only,
       count(*) FILTER (WHERE all_skills >= 5) - count(*) FILTER (WHERE english_skills >= 5) AS registries_lost
FROM big_repos;


-- 6. How many registry skills were dropped later (stage 4) because their newest version is not English?
--    Expected: 11,026
SELECT reason,
       count(*) AS lineages
FROM lineages_dropped
GROUP BY reason;


-- 7. Has anyone filled in the hand-check sheet yet?
--    Expected: 120 rows (40 per group), 0 filled in
SELECT is_english,
       language = 'unknown'           AS is_unknown,
       count(*)                       AS rows,
       count(human_is_english)        AS filled_in
FROM language_check_sample
GROUP BY ALL
ORDER BY ALL;


-- Not a query here: the 1,491-text test of the 75% rule (99.7% agreement) and the
-- "11% of English skills" finding came from a one-off run of the language detector
-- on a sample. They cannot be redone in SQL.
