-- Stage 2b: SKILL.md counts from each candidate repo's own tree.
-- Run views.sql first. Each query below stands alone. Run them one at a time.
--
-- Numbers below are the 2026-11-06 run: 6,834 candidates, all finished.
-- A later run changes these counts. Stage 2c has to be rebuilt to match.


-- 1. How did each tree check finish?
--    ok: the SKILL.md count is filled in.
--    gone: the repo is deleted or private. empty: the default branch has no commit.
--    timeout and error are tried again with --retry-failed.
--    Expected: 6,834 checked · ok 6,777 · gone 57 · empty 0 · timeout 0 · error 0
SELECT status,
       count(*)              AS repos,
       count(tree_skill_md)  AS with_a_count,
       count(head_commit)    AS with_a_commit
FROM registry_tree_counts
GROUP BY status
ORDER BY repos DESC;


-- 2. How many checked repos hold 5 or more SKILL.md files?
--    Stage 2c promotes an ok row at 5 or more. A failed check is not promoted.
--    Expected: promoted 1,240 · ok but under 5: 5,537 · not ok: 57
SELECT count(*) FILTER (WHERE status = 'ok' AND tree_skill_md >= 5) AS promoted,
       count(*) FILTER (WHERE status = 'ok' AND tree_skill_md < 5)  AS ok_under_5,
       count(*) FILTER (WHERE status <> 'ok')                       AS not_ok
FROM registry_tree_counts;


-- 3. The ten repos with the most SKILL.md files in their own tree.
SELECT repo,
       tree_skill_md,
       head_commit,
       round(seconds, 2) AS seconds,
       checked_at
FROM registry_tree_counts
WHERE status = 'ok'
ORDER BY tree_skill_md DESC, repo
LIMIT 10;


-- 4. Two widely copied repos the dataset under-counted.
--    Expected: obra/superpowers 15 SKILL.md files · anthropics/skills 20
SELECT repo,
       status,
       tree_skill_md,
       head_commit,
       message
FROM registry_tree_counts
WHERE repo IN ('obra/superpowers', 'anthropics/skills')
ORDER BY repo;


-- 5. A short look at the saved rows.
SELECT *
FROM registry_tree_counts
LIMIT 10;
