SET VARIABLE repos = '/Users/home/workspace/School/Advanced_Software_Engineering/Project/work/SWE-Group-Project--Project-3/src/data/gitskills_data/data/repos/*.parquet';
SET VARIABLE artifacts = '/Users/home/workspace/School/Advanced_Software_Engineering/Project/work/SWE-Group-Project--Project-3/src/data/gitskills_data/data/artifacts/*.parquet';
SET VARIABLE artifact_siblings = '/Users/home/workspace/School/Advanced_Software_Engineering/Project/work/SWE-Group-Project--Project-3/src/data/gitskills_data/data/artifact_siblings/*.parquet';
SET VARIABLE mining_runs = '/Users/home/workspace/School/Advanced_Software_Engineering/Project/work/SWE-Group-Project--Project-3/src/data/gitskills_data/data/mining_runs/*.parquet';

DESCRIBE select * from read_parquet(getvariable('artifacts'));
DESCRIBE select * from read_parquet(getvariable('artifact_siblings'));
DESCRIBE select * from read_parquet(getvariable('mining_runs'));
DESCRIBE select * from read_parquet(getvariable('repos'));

SELECT * FROM read_parquet(getvariable('artifacts')) LIMIT 5;
select count(*) from read_parquet(getvariable('artifacts'));
select count(*) from read_parquet(getvariable('artifact_siblings'));
select count(*) from read_parquet(getvariable('mining_runs'));
select count(*) from read_parquet(getvariable('repos'));

SELECT a.*, r.stars, r.is_fork, r.license
FROM read_parquet(getvariable('artifacts')) a
JOIN read_parquet(getvariable('repos')) r
  ON a.repo_full_name = r.full_name
WHERE a.dedup_primary = 1
LIMIT 20;

select * from read_parquet(getvariable('repos')) a limit 20;

-- Total number of artifacts, number of primary artifacts, and number of unique file_sha values
SELECT COUNT(*), SUM(dedup_primary), COUNT(DISTINCT file_sha) FROM read_parquet(getvariable('artifacts'));

SELECT file_sha, sum(dedup_primary) as primary_count from read_parquet(getvariable('artifacts')) group by file_sha having primary_count <> 1 limit 20;

-- File name variants
select filename, count(*) as variant_count from read_parquet(getvariable('artifacts')) group by filename order by variant_count desc limit 20;

select file_sha, count(*) from read_parquet(getvariable('artifacts')) group by file_sha having count(*) > 1 order by count(*) desc limit 20;

SELECT cp, COUNT(*)
FROM (SELECT COUNT(*) AS cp          -- inner: one row per distinct content
      FROM read_parquet(getvariable('artifacts'))
      GROUP BY file_sha)             --        cp = how many files share this hash
GROUP BY cp                          -- outer: how many contents have cp copies
ORDER BY cp;


SELECT file_sha,
     COUNT(*)                        AS cp,   -- files
     COUNT(DISTINCT repo_full_name)  AS nr    -- repos
FROM read_parquet(getvariable('artifacts'))
GROUP BY file_sha
limit 10;

select count(*) from read_parquet(getvariable('artifacts')) where content is null;
select file_sha from read_parquet(getvariable('artifacts')) where content is null and dedup_primary = 1 limit 10;
select * from read_parquet(getvariable('artifacts')) where file_sha = 'a8c3f80c3f3d116fe870e16730e5827e7d795508' and dedup_primary = 1 limit 10;
select distinct(file_sha) from read_parquet(getvariable('artifacts')) where content is null;

select count(*) from read_parquet(getvariable('artifacts')) where dedup_primary = 0 and (content is not null or content <> '');
select count(*) from read_parquet(getvariable('artifacts')) where dedup_primary = 0 and (content is null or content = '');
select count(*) from read_parquet(getvariable('artifacts')) where dedup_primary = 1 and (content is not null or content <> '');

SELECT MAX(name)                       AS name,
       COUNT(*)                        AS cp,
       COUNT(DISTINCT repo_full_name)  AS repos
FROM read_parquet(getvariable('artifacts'))
GROUP BY file_sha
ORDER BY cp DESC;

SELECT MAX(a.name) AS name,
       COUNT(DISTINCT a.repo_full_name) AS repos,
       COUNT(DISTINCT r.owner)          AS owners,
       COUNT(*)                         AS files
FROM read_parquet(getvariable('artifacts')) a JOIN read_parquet(getvariable('repos')) r ON r.full_name = a.repo_full_name
GROUP BY a.file_sha
ORDER BY owners DESC;


SELECT a.file_sha,
       MAX(a.name)                      AS name,      -- only the representative has a name
       COUNT(*)                         AS files,
       COUNT(DISTINCT a.repo_full_name) AS repos,
       COUNT(DISTINCT r.owner)          AS owners,
       MAX(CASE WHEN a.dedup_primary=1 THEN a.location_class END) AS rep_location,
       MAX(CASE WHEN a.dedup_primary=1 THEN a.body_chars END)     AS body_chars,
       MAX(CASE WHEN a.dedup_primary=1 THEN a.has_scripts END)    AS has_scripts,
       MAX(CASE WHEN a.dedup_primary=1 THEN a.frontmatter_valid END) AS fm_valid,
       MAX(r.stars)                     AS max_stars
FROM read_parquet(getvariable('artifacts')) a
LEFT JOIN read_parquet(getvariable('repos')) r ON r.full_name = a.repo_full_name
GROUP BY a.file_sha;


select
    a.file_sha,
    count(*) as files
FROM read_parquet(getvariable('artifacts')) a
LEFT JOIN read_parquet(getvariable('repos')) r ON r.full_name = a.repo_full_name
GROUP BY a.file_sha;

SELECT MAX(a.name) AS name,
       COUNT(DISTINCT a.repo_full_name) AS repos,
       COUNT(DISTINCT r.owner)          AS owners,
       COUNT(*)                         AS files_per_skill
FROM read_parquet(getvariable('artifacts')) a JOIN read_parquet(getvariable('repos')) r ON r.full_name = a.repo_full_name
GROUP BY a.file_sha
ORDER BY owners DESC

SELECT COUNT(*),  SUM(n)
FROM (SELECT lower(name) AS nm, COUNT(*) AS n
      FROM read_parquet(getvariable('artifacts'))
      WHERE dedup_primary = 1 AND name <> ''
      GROUP BY nm
      HAVING n > 1);

select * from
(SELECT lower(name) AS nm, COUNT(*) AS n
      FROM read_parquet(getvariable('artifacts'))
      WHERE dedup_primary = 1 AND name <> ''
      GROUP BY nm
      HAVING n > 1) where nm = 'frontend-design';

SELECT lower(name) AS nm, COUNT(*) AS n
      FROM read_parquet(getvariable('artifacts'))
      WHERE dedup_primary = 1 AND name <> ''
      GROUP BY nm
      ORDER BY n DESC;


SELECT file_sha, repo_full_name, body_chars,
    substr(description, 1, 120) AS descr
FROM read_parquet(getvariable('artifacts'))
WHERE dedup_primary = 1 AND lower(name) = 'skill-creator'
ORDER BY body_chars;

SELECT file_sha, repo_full_name, body_chars,
       substr(description, 1, 120) AS descr,
       substr(content, 1, 400)     AS start_of_text
FROM read_parquet(getvariable('artifacts'))
WHERE dedup_primary = 1 AND lower(name) = 'skill-creator'
ORDER BY random()
LIMIT 10;