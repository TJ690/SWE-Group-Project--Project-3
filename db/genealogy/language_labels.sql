-- Language labels: the numbers in docs/skill_genealogy/report/01_skill_language_report.md.
-- Run views.sql first. Each query below stands alone. Run them one at a time.
--
-- A "text" is one distinct skill content (one file_sha). The same text saved in
-- many repos is one text but many files.
-- English means the label is 'en' or 'unknown' (too short or unclear to tell).


-- 1. How many distinct skill texts were labelled?
--    Expected: 1,877,981
SELECT count(*) AS texts
FROM skill_language;


-- 2. English and not-English: texts, share of texts, files, repositories,
--    and files per text.
--    A repository with both kinds of text is counted on both sides.
--    Expected: English 1,633,285 texts (87.0%), 3,451,004 files, 251,416 repos, 2.1 files/text
--              not English 244,696 texts (13.0%), 346,113 files, 45,999 repos, 1.4 files/text
--              English files are 90.9% of all files
SELECT is_english,
       count(DISTINCT skill_language.file_sha)                          AS texts,
       round(100.0 * count(DISTINCT skill_language.file_sha)
             / sum(count(DISTINCT skill_language.file_sha)) OVER (), 1) AS percent_of_texts,
       count(*)                                                          AS files,
       round(100.0 * count(*) / sum(count(*)) OVER (), 1)                AS percent_of_files,
       count(DISTINCT artifacts.repo_full_name)                          AS repos,
       round(count(*) * 1.0 / count(DISTINCT skill_language.file_sha), 1) AS files_per_text
FROM skill_language
JOIN artifacts ON artifacts.file_sha = skill_language.file_sha
GROUP BY is_english
ORDER BY is_english DESC;


-- 3. How many repositories hold English texts, non-English texts, both, or either?
--    Expected: English 251,416 · non-English 45,999 · both 15,215 · either 282,200
WITH repo_sides AS (
    SELECT artifacts.repo_full_name                AS repo,
           bool_or(skill_language.is_english)     AS has_english,
           bool_or(NOT skill_language.is_english) AS has_non_english
    FROM artifacts
    JOIN skill_language ON artifacts.file_sha = skill_language.file_sha
    GROUP BY artifacts.repo_full_name
)
SELECT count(*) FILTER (WHERE has_english)                     AS repos_with_english,
       count(*) FILTER (WHERE has_non_english)                 AS repos_with_non_english,
       count(*) FILTER (WHERE has_english AND has_non_english) AS repos_with_both,
       count(*)                                                 AS repos_with_either
FROM repo_sides;


-- 4. Texts per language label, biggest first, with the typical text length.
--    'unknown' = fewer than 20 letters or the detector was unsure (counts as English).
--    'other'   = a script the detector does not cover.
--    Expected: en 1,624,923 (median 4,897) · zh 105,142 · ja 30,331 · de 29,634
--              · pt 18,889 · ko 18,226 · es 15,465 · unknown 8,362 (median 19)
--              · fr 7,480 · ru 4,863 · tr 3,867 · vi 2,982 · ar 2,446 · nl 1,377
--              · it 1,233 · pl 965 · id 779 · th 550 · other 234 · uk 189 · fa 28 · hi 16
SELECT language,
       is_english,
       count(*)           AS texts,
       median(body_chars) AS median_body_chars
FROM skill_language
GROUP BY language, is_english
ORDER BY texts DESC;


-- 5. How many registries would pass without the English filter, and how many with it?
--    Same rule both times (100+ stars, not a fork, 5+ different skills). Only the skill count differs.
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


-- 6. Has anyone filled in the hand-check sheet yet?
--    Expected: 120 rows (40 en, 40 unknown, 40 other), 0 filled in
SELECT is_english,
       language = 'unknown'    AS is_unknown,
       count(*)                AS rows,
       count(human_is_english) AS filled_in
FROM language_check_sample
GROUP BY ALL
ORDER BY ALL;
