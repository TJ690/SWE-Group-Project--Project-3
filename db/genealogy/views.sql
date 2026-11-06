-- Views for the genealogy queries. Run this file first, then any other file in this folder.
-- Paths are from the repository root.

-- One row per skill file in the dataset (repo, path, text, file_sha).
CREATE OR REPLACE VIEW artifacts AS
SELECT * FROM 'src/data/gitskills_data/data/artifacts/*.parquet';

-- One row per repository (name, owner, stars, fork flag).
CREATE OR REPLACE VIEW repos AS
SELECT * FROM 'src/data/gitskills_data/data/repos/*.parquet';

-- One row per distinct skill text, with its detected language and an is_english flag.
CREATE OR REPLACE VIEW skill_language AS
SELECT * FROM 'src/data/skill_genealogy/skill_language.parquet';

-- Registry skills (lineages) that were dropped, with the reason (for example not_english).
CREATE OR REPLACE VIEW lineages_dropped AS
SELECT * FROM 'src/data/skill_genealogy/lineages_dropped.parquet';

-- The 120 texts picked for a person to check the language label by hand.
CREATE OR REPLACE VIEW language_check_sample AS
SELECT * FROM 'src/data/skill_genealogy/language_check_sample.csv';
