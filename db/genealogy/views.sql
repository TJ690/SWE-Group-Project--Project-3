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

-- The 120 texts picked for a person to check the language label by hand.
CREATE OR REPLACE VIEW language_check_sample AS
SELECT * FROM 'src/data/skill_genealogy/language_check_sample.csv';

-- Stage 2b. One row per candidate repo whose own tree was checked.
CREATE OR REPLACE VIEW registry_tree_counts AS
SELECT * FROM 'src/data/skill_genealogy/registry_tree_counts.parquet';

-- Stage 2c. Repos that pass the registry rule, including tree promotions.
CREATE OR REPLACE VIEW registries AS
SELECT * FROM 'src/data/skill_genealogy/registries.parquet';

-- Stage 2c. One role for every repository in the dataset.
CREATE OR REPLACE VIEW repo_roles AS
SELECT * FROM 'src/data/skill_genealogy/repo_roles.parquet';

-- Stage 2c. The fixed 30-registry sample.
CREATE OR REPLACE VIEW dev_registries AS
SELECT * FROM 'src/data/skill_genealogy/dev_registries.parquet';

-- Stage 3. One row per file change in a registry skill folder.
-- A --dev run writes the same three files under src/data/skill_genealogy_dev/registry_history/.
CREATE OR REPLACE VIEW registry_history_changes AS
SELECT * FROM 'src/data/skill_genealogy/registry_history/registry_history_changes.parquet';

-- Stage 3. One row per blob. SKILL.md and small script texts, each stored once.
CREATE OR REPLACE VIEW registry_history_texts AS
SELECT * FROM 'src/data/skill_genealogy/registry_history/registry_history_texts.parquet';

-- Stage 3. One status per registry.
CREATE OR REPLACE VIEW registry_history_status AS
SELECT * FROM 'src/data/skill_genealogy/registry_history/registry_history_status.parquet';
