-- Stage 3: git history of each registry's skill folders.
-- Run views.sql first. Each query below stands alone. Run them one at a time.
--
-- These tables are written by download_registry_history. The views read the full
-- run under src/data/skill_genealogy/registry_history/. For a --dev run, point
-- the three stage 3 views at src/data/skill_genealogy_dev/registry_history/.
--
-- Text previews keep the first 400 characters. The texts table holds the full body.


-- 1. How did each registry finish?
--    ok: skill-folder history was saved.
--    gone: the repo is deleted, private, or asked for a login.
--    empty: the default branch has no commit.
--    timeout and error are tried again with --retry-failed.
SELECT status,
       count(*)            AS repos,
       sum(skill_folders)  AS skill_folders,
       sum(changes)        AS changes,
       sum(not_ours)       AS not_ours,
       sum(after_copy)     AS after_copy,
       sum(twin)           AS twin
FROM registry_history_status
GROUP BY status
ORDER BY repos DESC;


-- 2. Repos that did not finish, and the reason.
SELECT repo,
       status,
       message,
       round(seconds, 1) AS seconds
FROM registry_history_status
WHERE status <> 'ok'
ORDER BY status, repo;


-- 3. The ten registries with the most skill folders.
--    follow_renames is false when the repo has more than 200 skill folders.
SELECT repo,
       skill_folders,
       changes,
       follow_renames,
       not_ours,
       after_copy,
       twin,
       round(seconds, 1) AS seconds
FROM registry_history_status
WHERE status = 'ok'
ORDER BY skill_folders DESC, repo
LIMIT 10;


-- 4. What kinds of file change were kept?
--    status is the git letter: A add, M modify, D delete, R rename, C copy.
--    history_role is own, former_folder (a name this skill used to have),
--    or copy_source (the folder this skill was copied from, before the copy).
SELECT status AS git_status,
       history_role,
       count(*)                                              AS rows,
       count(*) FILTER (WHERE is_skill_md)                  AS skill_md_rows,
       count(*) FILTER (WHERE is_script)                    AS script_rows
FROM registry_history_changes
GROUP BY git_status, history_role
ORDER BY rows DESC;


-- 5. The ten skill folders with the most SKILL.md commits.
--    commit_seq 1 is the oldest kept commit in that folder.
SELECT repo,
       skill_folder,
       count(DISTINCT commit) AS skill_md_commits,
       min(commit_date)       AS first_commit,
       max(commit_date)       AS last_commit
FROM registry_history_changes
WHERE is_skill_md
GROUP BY repo, skill_folder
ORDER BY skill_md_commits DESC, repo, skill_folder
LIMIT 10;


-- 6. Every kept SKILL.md version of the folder from query 5, with the start of the text.
--    new_sha is the blob after the change. A delete has no new text.
WITH top_folder AS (
    SELECT repo, skill_folder
    FROM registry_history_changes
    WHERE is_skill_md
    GROUP BY repo, skill_folder
    ORDER BY count(DISTINCT commit) DESC, repo, skill_folder
    LIMIT 1
)
SELECT changes.commit_seq,
       changes.commit_date,
       changes.status AS git_status,
       changes.subject,
       changes.history_role,
       length(texts.text) AS chars,
       substring(texts.text, 1, 400) AS text_start
FROM registry_history_changes AS changes
JOIN top_folder USING (repo, skill_folder)
LEFT JOIN registry_history_texts AS texts ON texts.sha = changes.new_sha
WHERE changes.is_skill_md
ORDER BY changes.commit_seq;


-- 7. Read one full SKILL.md. Paste a sha from query 6 into the WHERE line.
SELECT sha,
       length(text) AS chars,
       text
FROM registry_history_texts
WHERE sha = 'paste-a-sha-from-query-6';


-- 8. Five small scripts, with the start of each text.
--    A script larger than 200,000 bytes is kept as a change row and has no text.
SELECT changes.repo,
       changes.skill_folder,
       changes.file_path,
       changes.commit_seq,
       changes.commit_date,
       length(texts.text) AS chars,
       substring(texts.text, 1, 400) AS text_start
FROM registry_history_changes AS changes
JOIN registry_history_texts AS texts ON texts.sha = changes.new_sha
WHERE changes.is_script
ORDER BY changes.repo, changes.skill_folder, changes.file_path, changes.commit_seq
LIMIT 5;


-- 9. Change keys stored on more than one row.
--    A key is repo, skill_folder, commit, and file_path. Expected: 0
SELECT count(*) AS duplicate_keys
FROM (
    SELECT repo, skill_folder, commit, file_path
    FROM registry_history_changes
    GROUP BY ALL
    HAVING count(*) > 1
);


-- 10. A short look at the saved rows. The text preview is the first 200 characters.
SELECT *
FROM registry_history_status
LIMIT 10;

SELECT repo, skill_folder, file_path, commit_seq, commit_date, status, history_role, subject
FROM registry_history_changes
LIMIT 10;

SELECT sha,
       length(text) AS chars,
       substring(text, 1, 200) AS text_start
FROM registry_history_texts
LIMIT 10;



select * from registry_history_changes limit 10;

select * from registry_history_texts limit 10;

select * from registry_history_status limit 10; 