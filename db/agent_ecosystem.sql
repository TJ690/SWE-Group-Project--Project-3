SET VARIABLE artifacts = '/Users/home/workspace/School/Advanced_Software_Engineering/Project/work/SWE-Group-Project--Project-3/src/data/gitskills_data/data/artifacts/*.parquet';

COPY (
SELECT repo_full_name, path, file_sha, location_class,
       CASE lower(regexp_extract(path, '(^|/)(\.[^/]+)/', 2))       -- first dot-folder in the path
         WHEN '.claude'      THEN 'Claude Code'
         WHEN '.agents'      THEN 'Shared (.agents)'
         WHEN '.agent'       THEN 'Shared (.agent)'
         WHEN '.github'      THEN 'GitHub Copilot'
         WHEN '.cursor'      THEN 'Cursor'
         WHEN '.codex'       THEN 'Codex'
         WHEN '.opencode'    THEN 'opencode'
         WHEN '.gemini'      THEN 'Gemini CLI'
         WHEN '.antigravity' THEN 'Antigravity'
         WHEN '.kiro'        THEN 'Kiro'
         WHEN '.windsurf'    THEN 'Windsurf'
         WHEN '.trae'        THEN 'Trae'
         WHEN '.qwen'        THEN 'Qwen Code'
         WHEN '.kilocode'    THEN 'Kilo Code'
         WHEN '.continue'    THEN 'Continue'
         WHEN '.factory'     THEN 'Factory'
         WHEN ''             THEN 'No agent folder'
         ELSE 'Other dot-folder'
       END AS agent_folder
FROM read_parquet(getvariable('artifacts'))
) TO '/Users/home/workspace/School/Advanced_Software_Engineering/Project/work/SWE-Group-Project--Project-3/src/data/generated_data/file_agents.parquet' (FORMAT parquet);

