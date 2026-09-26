SELECT COUNT(*) FROM artifacts;                            -- 29,786
SELECT COUNT(*) FROM artifacts WHERE dedup_primary = 1;    -- 13,000
SELECT COUNT(DISTINCT file_sha) FROM artifacts;            -- 13,000
SELECT COUNT(*) FROM artifact_siblings;
SELECT COUNT(*) FROM repos;                                -- 11,841

select
    file_sha,
    SUM(dedup_primary) as reps
from artifacts
group by file_sha
having reps <> 1;

select * from artifacts limit 10;

-- Number of copies
select file_sha, copies from
(select file_sha, COUNT(*) as copies from artifacts
GROUP BY file_sha) T
order by copies desc limit 10;

SELECT location_class, COUNT(*) FROM artifacts
WHERE dedup_primary = 1 GROUP BY location_class;

SELECT copies, COUNT(*) AS n_contents FROM (
  SELECT file_sha, COUNT(*) AS copies FROM artifacts GROUP BY file_sha
) GROUP BY copies ORDER BY copies;

SELECT cp, COUNT(*)
FROM (SELECT COUNT(*) AS cp          -- inner: one row per distinct content
      FROM artifacts
      GROUP BY file_sha)             --        cp = how many files share this hash
GROUP BY cp                          -- outer: how many contents have cp copies
ORDER BY cp;