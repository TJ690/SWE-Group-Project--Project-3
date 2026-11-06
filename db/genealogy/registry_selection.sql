-- Stage 2c: registries, roles, and the 30-registry sample.
-- Run views.sql first. Each query below stands alone. Run them one at a time.
--
-- These tables are written by select_registries --force. They read the stage 2b
-- tree counts and do not clone anything.
--
-- Numbers below are the 2026-11-06 run.


-- 1. How did each registry pass?
--    dataset: 5 or more English skills in the dataset.
--    repo_tree: 1–4 English skills in the dataset, and 5 or more SKILL.md files
--    in the repo itself.
--    Expected: dataset 3,566 · repo_tree 1,240 · total 4,806
SELECT registry_basis,
       count(*)               AS registries,
       median(stars)          AS median_stars,
       median(english_skills) AS median_english_skills,
       median(tree_skill_md)  AS median_tree_skill_md
FROM registries
GROUP BY registry_basis
ORDER BY registries DESC;


-- 2. The ten tree-promoted registries with the most stars.
--    english_skills is the dataset count. tree_skill_md is the repo's own count.
SELECT repo,
       stars,
       english_skills,
       tree_skill_md,
       registry_basis
FROM registries
WHERE registry_basis = 'repo_tree'
ORDER BY stars DESC, repo
LIMIT 10;


-- 3. One role for every repository.
--    registry passes the rule and is not a hub.
--    registry_hub passes and is a hub.
--    hub is a hub that does not pass (for example, fewer than 100 stars).
--    repository is everything else.
--    Expected: repository 277,389 · registry 4,806 · hub 5 · registry_hub 0
SELECT repo_role,
       count(*) AS repos
FROM repo_roles
GROUP BY repo_role
ORDER BY repos DESC;


-- 4. The hubs, and why each one did or did not become a registry.
--    A hub needs hub_share >= 0.25 and at least 5 dated English texts.
SELECT repo,
       stars,
       is_fork,
       english_skills,
       hub_share,
       is_registry,
       repo_role
FROM repo_roles
WHERE is_hub
ORDER BY hub_share DESC, repo;


-- 5. The fixed 30-registry sample, ten from each size group.
--    The same seed returns this list. size_group is small, medium, then large.
SELECT size_group,
       repo,
       english_skills,
       is_hub
FROM dev_registries
ORDER BY CASE size_group
             WHEN 'small' THEN 0
             WHEN 'medium' THEN 1
             ELSE 2
         END,
         repo;


-- 6. Forks and repos with no star count must not be registries.
--    Expected: 0
SELECT count(*) AS registries_that_should_not_pass
FROM repo_roles
WHERE is_registry
  AND (is_fork <> 0 OR stars IS NULL OR stars < 100);


-- 7. A short look at the saved rows.
SELECT *
FROM registries
LIMIT 10;

SELECT *
FROM repo_roles
LIMIT 10;
