SET VARIABLE artifact_siblings = 'src/data/gitskills_data/data/artifact_siblings/*.parquet';
SET VARIABLE siblings = 'src/data/generated_data/sibling_files.parquet';

COPY (
SELECT s.repo_full_name, s.artifact_path, s.entry_name, s.entry_size, s.entry_sha,
       s.skipped_reason,
       split_part(s.entry_name, '/', 1)                                  AS top_folder,
       CASE WHEN strpos(s.entry_name, '/') = 0 THEN '(skill root)'
            ELSE split_part(s.entry_name, '/', 1) END                    AS subfolder,
       lower(regexp_extract(s.entry_name, '\.([A-Za-z0-9]+)$', 1))       AS ext,
       CASE
         WHEN lower(regexp_extract(s.entry_name, '\.([A-Za-z0-9]+)$', 1))
              IN ('py','sh','bash','zsh','js','mjs','cjs','ts','ps1','rb','go','rs','php','pl','lua','java','kt','swift','c','cpp','cs','r')
              THEN 'script / code'
         WHEN lower(regexp_extract(s.entry_name, '\.([A-Za-z0-9]+)$', 1))
              IN ('md','mdx','txt','rst','adoc') THEN 'documentation'
         WHEN lower(regexp_extract(s.entry_name, '\.([A-Za-z0-9]+)$', 1))
              IN ('json','yaml','yml','toml','csv','tsv','xml','ini','jsonl','sql') THEN 'data / config'
         WHEN lower(regexp_extract(s.entry_name, '\.([A-Za-z0-9]+)$', 1))
              IN ('png','jpg','jpeg','gif','svg','webp','ico','ttf','otf','woff','woff2','pdf','mp3','mp4','wav') THEN 'media / asset'
         WHEN lower(regexp_extract(s.entry_name, '\.([A-Za-z0-9]+)$', 1))
              IN ('html','css','scss','jsx','tsx','vue','hbs','j2','jinja','tmpl') THEN 'web / template'
         WHEN lower(regexp_extract(s.entry_name, '\.([A-Za-z0-9]+)$', 1)) = '' THEN 'no extension'
         ELSE 'other'
       END AS file_category
FROM read_parquet(getvariable('artifact_siblings')) s
WHERE s.entry_type = 'file'
) TO 'src/data/generated_data/sibling_files.parquet' (FORMAT parquet, COMPRESSION zstd);


