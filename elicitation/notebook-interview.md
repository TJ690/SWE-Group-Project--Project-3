## Q1: What is a gitskill? Which files/folders give information of gitskills? How is the dataset created?
### 1\. What is a GitSkill?

An **agent skill** (or GitSkill) is a standardized folder containing a **SKILL.md** file that provides natural-language instructions for an AI language-model agent (such as Claude Code), optionally accompanied by executable scripts and reference documents[1]. Anthropic introduced the format in October 2025 as an open specification[1].

When an agent receives a task, it probabilistically compares the user's request against the skill's description and decides whether to load the skill at run time[2]. In the context of the MSR Mining Challenge, **GitSkills** specifically refers to a large-scale dataset of 3,797,117 `SKILL.md` files collected across 282,200 public GitHub repositories in July 2026[1].

---

### 2\. Which Files and Folders Provide Information on GitSkills?

Information about agent skills exists both within individual project repositories and inside the structured tables of the GitSkills dataset:

#### **A. Within a Repository / Skill Directory**

* **SKILL.md**: The primary file containing **YAML front matter** (specifying metadata like `name` and `description`) followed by a **Markdown body** detailing the instructions[3][8].
* **Bundled Folders &amp; Files**: Optional subdirectories stored alongside `SKILL.md` containing executable scripts (e.g., `scripts/`), templates (e.g., `templates/`), or reference documentation (e.g., `references/`)[1].
* **Directory Paths**: Skills commonly reside in canonical paths like `.claude/skills/

---
### References
1. **Source Title 1** - GitHub - giuseppedestefanis/gitskills-sample | GitSkills — sample for the MSR 2027 Mining Challenge proposal
2. **Source Title 2** - GitSkills: A Dataset of Agent Skills on GitHub | Zenodo | Description
3. **Source Title 3** - Gitskills_Preprint.pdf | HIGH-LEVEL OVERVIEW Paragraph 1
4. **Source Title 5** - GitHub - giuseppedestefanis/gitskills-sample | Description Paragraph 2
5. **Source Title 6** - Gitskills_Preprint.pdf | HIGH-LEVEL OVERVIEW Paragraph 3
6. **Source Title 7** - MSR 2027 - Mining Challenge - MSR 2027 | Gitskills
7. **Source Title 8** - Gitskills_Preprint.pdf | Web Artifacts Builder
8. **Source Title 9** - mvaccargiu/gitskills · Datasets at Hugging Face | references/safety.md
9. **Source Title 10** - mvaccargiu/gitskills · Datasets at Hugging Face
10. **Source Title 11** - GitHub - giuseppedestefanis/gitskills-sample | Schema
11. **Source Title 12** - GitHub - giuseppedestefanis/gitskills-sample | Descriptive Statistics
12. **Source Title 13** - Gitskills_Preprint.pdf | Deduplication
13. **Source Title 14** - Gitskills_Preprint.pdf | HIGH-LEVEL OVERVIEW Paragraph 4
---

## Q2: What is the license of the dataset? Was there any other criteria of project selection from github apart from having skill.md? Github has millions of dataset. How did the data collection team identify which projects have skill.md and download metadata of only those projects?
### 1\. Dataset License

* **Core Dataset License**: The metadata and aggregation of the GitSkills dataset are released under the **Creative Commons Attribution 4.0 International (CC-BY-4.0)** license[1][2].
* **File Content License**: The full-text instruction contents reproduced in the dataset originate from public GitHub repositories and remain subject to the individual licenses of their original repositories[2]. Researchers reusing specific file contents should consult the `repos.license` field or the original repository[2].

---

### 2\. Project Selection Criteria

The data collection team **did not filter repositories based on manual criteria or project popularity**; every public repository containing a file matching `SKILL.md` (or case variations) was included[3].

However, because discovery relied on the GitHub Code Search API, the dataset inherits GitHub's intrinsic indexing constraints[4]:

* **Public Repositories Only**: Private repositories were excluded[7].
* **Default Branches**: GitHub code search indexes files located only on default repository branches (e.g., `main` or `master`)[4].
* **File Size Cap**: Only files smaller than **384 KB** are indexed by GitHub code search[4].
* **Repository Activity &amp; Size**: Indexed repositories must be recently active and contain fewer than **500,000 files**[4].
* **Forks Constraint**: GitHub indexes a repository fork **only if it has more stars than its parent repository** (as a result, out of 282,200 repositories in the dataset, only 3 are forks)[4].

---

### 3\. Identifying and Gathering Metadata for `SKILL.md` Projects

To scan GitHub's vast ecosystem and isolate only relevant repositories, the collection team built a read-only pipeline using the **GitHub Code Search, REST, and GraphQL APIs**, along with the **raw-content CDN**[8].

The identification and collection process ran in three main stages:

#### **Stage 1: Discovery &amp; Search Partitioning**

* **Targeting** **SKILL.md**: Since agent tools look for files specifically named `SKILL.md` (or case-insensitive variants like `skill.md`), the team used exact file basename queries[4][13].
* **Overcoming the 1,000-Result API Cap**: The GitHub Code Search API restricts queries to a maximum of 1,000 results, and its initial result count estimate was inaccurate (reporting \~349,000 matches for the query when over 3.8 million files actually existed)[5].
* **Recursive File-Size Partitioning**: To bypass the 1,000-result cap, the pipeline **recursively partitioned the search query by file size ranges**[5]. Any size range returning more than 1,000 results was split into smaller sub-ranges until every matching file could be retrieved completely[5]. This strategy identified **3,797,117 file occurrences across 282,200 repositories**[14][15].

#### **Stage 2: Deduplication**

* Discovered files were grouped by their Git content hash (`file_sha`) into **1,877,981 distinct contents**[5].
* For each distinct content group, **one representative file** was chosen for detailed enrichment (preferring files in standard paths like `.claude/skills/`), while all duplicate copies were retained to preserve location data[5].

#### **Stage 3: Targeted Metadata Enrichment**

* Rather than downloading unnecessary data across millions of unrelated GitHub projects, the pipeline fetched repository and commit metadata **only for the identified** **SKILL.md** **projects**:
  * **Repository Metadata**: Star count, primary language, fork status, license, creation date, and last push date were fetched via GitHub REST/GraphQL APIs and stored in the `repos` table[12][18].
  * **File &amp; Sibling Contents**: The raw `SKILL.md` text, parsed YAML front matter, and bundled sibling files (up to size caps) were fetched via the raw-content CDN and stored in the `artifacts` and `artifact_siblings` tables[5].
  * **Commit History**: First- and last-commit histories were sampled and anonymized for representative skills[5].

---
1. **Source Title 1** - GitSkills: A Dataset of Agent Skills on GitHub | Zenodo | Target URL, License, and Citation
2. **Source Title 2** - mvaccargiu/gitskills · Datasets at Hugging Face | License
3. **Source Title 3** - Gitskills_Preprint.pdf | GitHub public repositories
4. **Source Title 4** - Gitskills_Preprint.pdf | Web Artifacts Builder Paragraph 4
5. **Source Title 5** - mvaccargiu/gitskills · Datasets at Hugging Face | Discovery
6. **Source Title 6** - GitHub - giuseppedestefanis/gitskills-sample | Two views of the full dataset
7. **Source Title 7** - mvaccargiu/gitskills · Datasets at Hugging Face | Anonymization
8. **Source Title 8** - GitSkills: A Dataset of Agent Skills on GitHub | Zenodo | Description Paragraph 4 & 5
9. **Source Title 9** - Gitskills_Preprint.pdf | High Level Overview Paragraph 4
10. **Source Title 10** - GitHub - giuseppedestefanis/gitskills-sample | Descriptive Statistics
11. **Source Title 11** - GitHub - giuseppedestefanis/gitskills-sample | How data was collected
12. **Source Title 12** - mvaccargiu/gitskills · Datasets at Hugging Face | artifact-repos & siblings
13. **Source Title 13** - Gitskills_Preprint.pdf | APPENDIX A DATASET CONSTRUCTION
14. **Source Title 14** - GitHub - giuseppedestefanis/gitskills-sample | GitSkills — sample for the MSR 2027 Mining Challenge proposal
15. **Source Title 15** - mvaccargiu/gitskills · Datasets at Hugging Face | GitSkills: A Dataset of Agent Skills on GitHub
16. **Source Title 16** - GitHub - giuseppedestefanis/gitskills-sample | Schema
17. **Source Title 17** - Gitskills_Preprint.pdf | GitHub public repositories
18. **Source Title 18** - GitHub - giuseppedestefanis/gitskills-sample | repos, artifact_siblings & mining_runs tables
---

## Q3: What was the timeframe of the github metadata that is present in the dataset?
The GitHub metadata in the GitSkills dataset covers the following specific timeframes:

* **Collection Snapshot**: The dataset represents a point-in-time snapshot collected in **July 2026**[1].
* **Commit History Range**: The recorded commit timestamps span from **2014 through July 2026**[2][5]. The earliest commits date back to 2014 because the case-insensitive search matched legacy files (such as lowercase `skill.md`) created long before the official format was established[5][6].
* **Agent Skill Format Era**: The bulk of agent skills in the specification format emerged between **October 2025** (when Anthropic introduced the open specification) and **July 2026** when data mining took place[1].
---
References:
1. **Source Title 1** - GitHub - giuseppedestefanis/gitskills-sample | GitSkills
2. **Source Title 2** - GitHub - giuseppedestefanis/gitskills-sample | How the data was collected
3. **Source Title 3** - GitSkills: A Dataset of Agent Skills on GitHub | Zenodo | Desription
4. **Source Title 4** - Gitskills_Preprint.pdf | HIGH-LEVEL OVERVIEW
5. **Source Title 5** - GitHub - giuseppedestefanis/gitskills-sample | Descriptive statistics
6. **Source Title 6** - Gitskills_Preprint.pdf | Web Artifacts Builder
7. **Source Title 7** - GitSkills: A Dataset of Agent Skills on GitHub | Zenodo | Description
8. **Source Title 8** - Gitskills_Preprint.pdf | HIGH-LEVEL OVERVIEW
---

## Q4: How did the dataset creators identify matching skills.md files among other repositories? How did they perform match of gitfile contents with all other repositories skills.md?
The dataset creators used a three-stage automated pipeline to discover, match, and deduplicate `SKILL.md` files across public GitHub repositories[1][2]:

---

### 1\. Identifying `SKILL.md` Files Across Repositories

Because AI agent tools look for files specifically named `SKILL.md` (or case variants), the collection team targeted exact basename queries using the **GitHub Code Search API**[3].

To identify these files at scale without missing data, they addressed key API constraints:

* **Overcoming the 1,000-Result Limit**: The GitHub Code Search API caps results at 1,000 matches per query, and its initial result estimate was inaccurate (\~349,000 estimated vs. over 3.8 million actual matches)[3][4].
* **Recursive File-Size Partitioning**: To bypass this cap, the discovery pipeline **recursively partitioned the search query by file size ranges**[2]. Any size range returning more than 1,000 matches was split into smaller sub-ranges until every matching file could be completely retrieved[3][4].
* **Indexing Boundaries**: GitHub's code search indexed default branches, files under 384 KB, active repositories with fewer than 500,000 files, and forks with more stars than their parent[5].

Through this process, the team identified **3,797,117 file occurrences** across 282,200 public GitHub repositories[3][8].

---

### 2\. Matching Git File Contents Across Repositories

To match and compare skill file contents across all repositories without downloading redundant data, the creators leveraged Git's built-in content addressing:

* **Grouping by Git Content Hash (** **file\_sha** **)**: Every file on Git/GitHub is identified by a deterministic Git blob SHA (`file_sha`), where identical bytes produce the exact same hash[9][10]. The creators grouped all 3.8 million discovered occurrences by their `file_sha`[3][8]. This revealed that **50.5% of all files are verbatim copies**, compressing the dataset into **1,877,981 distinct skill contents**[3].
* **Selecting a Representative File**: For each group of identical content hashes, **one representative file** was selected for full text download and metadata enrichment[2]. The selection algorithm preferred files located in standard canonical paths (such as `.claude/skills/

---
1. **Source Title 1** - Gitskills_Preprint.pdf
2. **Source Title 2** - mvaccargiu/gitskills · Datasets at Hugging Face | Discovery
3. **Source Title 3** - GitHub - giuseppedestefanis/gitskills-sample | How the data was collected
4. **Source Title 4** - Gitskills_Preprint.pdf | Appendix
5. **Source Title 5** - Gitskills_Preprint.pdf | Web Artifacts Builder
6. **Source Title 6** - GitHub - giuseppedestefanis/gitskills-sample | Two Views of the Full Dataset
7. **Source Title 7** - mvaccargiu/gitskills · Datasets at Hugging Face | Collection Methodology
8. **Source Title 8** - Gitskills_Preprint.pdf | High-Level Overview
9. **Source Title 9** - GitHub - giuseppedestefanis/gitskills-sample | Data Schema
10. **Source Title 10** - Gitskills_Preprint.pdf | Internal Structure
11. **Source Title 11** - GitHub - giuseppedestefanis/gitskills-sample | Descriptive Statistics
12. **Source Title 12** - Gitskills_Preprint.pdf | Deduplication
13. **Source Title 13** - GitSkills: A Dataset of Agent Skills on GitHub | Zenodo | Description
14. **Source Title 14** - Gitskills\_Preprint.pdf | APPENDIX A DATASET CONSTRUCTION
---

## Q5: Explain about all the schemas involved. Describe each table and how they can be joined? What is estimated size of each table and number of records in each table. Is there a possibility of orphan records in any of the table. If there is no documentation about help me with queries to identify orphaned rows in each table.
### 1\. Overview of Schemas Involved

The **GitSkills** dataset is distributed as a single, self-contained **SQLite database** (`agent_skills_release.db` / `agent_skills_sample.db`) and mirrored as Parquet files on Hugging Face[1][2]. It comprises **4 tables** designed around Git content addressing and deduplication[1]:

1. **artifacts**: The core table containing one row for every discovered `SKILL.md` file occurrence across GitHub repositories[1][5].
2. **repos**: Stores repository-level metadata for every GitHub repository hosting one or more discovered skills[1][6].
3. **artifact\_siblings**: Stores metadata and content for bundled files (scripts, templates, schemas, and reference docs) stored alongside representative skill files[1].
4. **mining\_runs**: A provenance and execution log tracking data collection runs[1][6].

---

### 2\. Table Descriptions &amp; Joins
#### **A. Table Descriptions**

* **artifacts** (Primary Key: `repo_full_name`, `path`)[5]:
  * **Location &amp; Identity**: `repo_full_name`, `path`, `filename`, `location_class` (e.g., canonical `.claude/skills/
  * **Deduplication**: `file_sha` (Git blob content hash) and `dedup_primary` (set to `1` for the single representative file selected per distinct content hash)[5].
  * **Content &amp; Front Matter**: `content` (full text, populated for representatives only), parsed YAML front matter (`name`, `description`), and `body_chars`[5].
  * **Folder Composition Summary**: `sibling_count`, `sibling_bytes`, `has_scripts`, `has_references`[5].
  * **Sampled Commit History**: `first_commit_at`, `last_commit_at`, `commit_count`, anonymized author codes (`first_commit_author`, `last_commit_author`), author types, and masked commit messages[5][9].
* **repos** (Primary Key: `full_name`)[6]:
  * Contains repository attributes: `full_name`, `owner`, `stars`, `forks`, `is_fork`, `language`, `license`, `description` (emails masked), `created_at`, `pushed_at`, and `metadata_fetched`[6].
* **artifact\_siblings** (Composite Foreign Key: `repo_full_name`, `artifact_path`)[6][10]:
  * Contains files bundled in the same folder as a representative skill: `repo_full_name`, `artifact_path` (joins to `artifacts.path`), `entry_name`, `entry_type` (`file` or `dir`), `entry_size`, `entry_sha`, `content` (text up to 100 KB), `content_fetched`, and `skipped_reason`[6].
* **mining\_runs**:
  * Collection log: `query`, start and end timestamps, and search result counts[6].

---

#### **B. How the Tables Join Together**

* **Joining** **artifacts** **and** **repos**: Connects skill files to their host repository metadata using `repo_full_name`[11].

```
SELECT a.filename, r.language, r.stars
FROM artifacts a
JOIN repos r ON a.repo_full_name = r.full_name;

```

* **Joining** **artifacts** **and** **artifact\_siblings**: Connects representative skills to their bundled files via a composite join on repository name and skill path[10].

```
SELECT a.name, s.entry_name, s.entry_type, s.entry_size
FROM artifact_siblings s
JOIN artifacts a ON a.repo_full_name = s.repo_full_name
                AND a.path = s.artifact_path
WHERE a.dedup_primary = 1;
```
* **mining\_runs**: An independent logging table; it does not share foreign key columns with the operational dataset tables[1][6].

---

### 3\. Estimated Table Sizes &amp; Record Counts

| Table Name             | Full Dataset Record Count[1][3] | Sample Dataset Record Count[3] | Key Characteristics[3]                                                                                       |
| ---------------------- | ----------------------------------- | -------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| **artifacts**          | **3,797,117 rows**                  | 29,786 rows                      | Represents every file occurrence; contains 1,877,981 distinct skill contents (`dedup_primary = 1`)[3][12]. |
| **artifact\_siblings** | **7,264,865 rows**                  | 47,829 rows                      | Bundled files/directories; 3,497,752 carry full text content[3].                                             |
| **repos**              | **282,200 rows**                    | 11,841 rows                      | Repository metadata covering 195,841 distinct owners[3][12].                                               |
| **mining\_runs**       | **7 rows**                          | 7 rows                           | Collection provenance log[1][3].                                                                           |

* **Overall Database Storage Size**:
  * **Full SQLite Database (** **agent\_skills\_release.db** **)**: **41 GB – 44.4 GB**[3][13].
  * **Hugging Face Parquet Mirror**: **\~13.4 GB** total compressed files[14].
  * **Sample SQLite Database (** **agent\_skills\_sample.db** **)**: **277 MB**[3].

---

### 4\. Possibility of Orphaned Records

In relational databases, orphaned records occur when child rows reference non-existent parent rows or when parent records exist without associated child data:

1. **artifacts** **referencing missing** **repos**: Occurs if a skill file exists in `artifacts`, but repository metadata fetching failed or was skipped for `repos`. (Note: During collection, \~0.9% of distinct files deleted from GitHub between discovery and retrieval were excluded[15]).
2. **repos** **without** **artifacts**: Repositories present in `repos` that lack matching rows in `artifacts` (e.g., if files were deleted before pipeline processing).
3. **artifact\_siblings** **referencing missing** **artifacts**: Sibling files referencing a `(repo_full_name, artifact_path)` pair that does not exist in `artifacts`.
4. **artifact\_siblings** **linked to non-primary artifacts**: Siblings should only be collected for primary representative skills (`dedup_primary = 1`)[6][7]. Any sibling record joining to an artifact with `dedup_primary = 0` represents a logic mismatch.

---

### 5\. SQL Queries to Identify Orphaned Rows

The dataset documentation does not explicitly detail orphaned rows; you can run the following standard SQLite diagnostic queries using `LEFT JOIN` and `WHERE ... IS NULL` to verify relational integrity:

#### **Query 1: Find artifacts without matching repository entries in** **repos**

```
SELECT a.repo_full_name, a.path
FROM artifacts a
LEFT JOIN repos r ON a.repo_full_name = r.full_name
WHERE r.full_name IS NULL;

```

#### **Query 2: Find repositories in** **repos** **that have no matching** **artifacts**

```
SELECT r.full_name
FROM repos r
LEFT JOIN artifacts a ON r.full_name = a.repo_full_name
WHERE a.repo_full_name IS NULL;

```

#### **Query 3: Find sibling entries in** **artifact\_siblings** **that point to non-existent skills in** **artifacts**

```
SELECT s.repo_full_name, s.artifact_path, s.entry_name
FROM artifact_siblings s
LEFT JOIN artifacts a ON s.repo_full_name = a.repo_full_name 
                     AND s.artifact_path = a.path
WHERE a.repo_full_name IS NULL;

```

#### **Query 4: Find sibling entries attached to non-representative skills (** **dedup\_primary != 1** **)**

```
SELECT s.repo_full_name, s.artifact_path, s.entry_name, a.dedup_primary
FROM artifact_siblings s
JOIN artifacts a ON s.repo_full_name = a.repo_full_name 
                AND s.artifact_path = a.path
WHERE a.dedup_primary != 1;

```

---

💡 Would you like to inspect specific query execution plans or analyze column distributions in Python using the Hugging Face Parquet mirror?

Does artifact\_siblings has siblings of skill discovered and placed in skill table or all skill identified in all repositories?

The **artifact\_siblings** table contains bundled sibling files **only for representative skills** (`dedup_primary = 1`), **not for all skill occurrences across all repositories**[1].

### Detailed Explanation:

1. **All Skill Occurrences in** **artifacts**: The `artifacts` table tracks **every single occurrence** of a `SKILL.md` file discovered across GitHub (3,797,117 occurrences across 282,200 repositories)[2].
2. **Folder Composition Collected Only for Representatives**: During the pipeline's deduplication stage, identical files were grouped by their Git content hash (`file_sha`) into 1,877,981 distinct contents[2][6]. The pipeline selected **one representative file per content group** (preferring standard paths like `.claude/skills/`) for full metadata enrichment[1][6]. Folder contents and sibling files (such as scripts, templates, and reference docs) were collected **exclusively for this single representative copy**[2].
3. **Implications for Queries**:
  * Sibling entries in `artifact_siblings` join back to `artifacts` on `(repo_full_name, artifact_path)`[1].
  * These joins link exclusively to the primary representative row where `dedup_primary = 1`[5][7].
  * Other repositories containing verbatim copies of the same `SKILL.md` file do not have their local directory contents recorded in `artifact_siblings`[3].

---
References:
1. **Source Title 1** - GitSkills: A Dataset of Agent Skills on GitHub | Zenodo | Description
2. **Source Title 2** - GitSkills: A Dataset of Agent Skills on GitHub | Zenodo | Description
3. **Source Title 3** - GitHub - giuseppedestefanis/gitskills-sample | Quickstart
4. **Source Title 4** - Gitskills_Preprint.pdf | Internal Structure
5. **Source Title 5** - GitHub - giuseppedestefanis/gitskills-sample | Schema
6. **Source Title 6** - GitHub - giuseppedestefanis/gitskills-sample | repos, artifact_siblings & mining_runs tables
7. **Source Title 7** - Gitskills_Preprint.pdf | Table # Records Content
8. **Source Title 8** - GitHub - giuseppedestefanis/gitskills-sample | Descriptive Statistics
9. **Source Title 9** - GitHub - giuseppedestefanis/gitskills-sample | Anonymisation
11. **Source Title 11** - GitHub - giuseppedestefanis/gitskills-sample | Example Queries
13. **Source Title 13** - GitSkills: A Dataset of Agent Skills on GitHub | Zenodo | File Size
14. **Source Title 14** - mvaccargiu/gitskills · Datasets at Hugging Face | contact
15. **Source Title 15** - GitHub - giuseppedestefanis/gitskills-sample | How data was collected
---

## Q6: Is there a way to identify the original repository of the representative skill. Meaning first time skill was documented in a repository. Can it be traced as the source repository of a git skill?

Yes, the original repository can be traced, but the representative skill (** **dedup\_primary = 1** **) in the dataset is not automatically the original source.
---
### 1. Representative Skill $\neq$ Original Source
When the dataset creators deduplicated skills by their content hash (file_sha), they selected one representative row per unique content group12.However, they explicitly note that the representative file is NOT assumed to be the original creator2. Instead, representative files were chosen based on structural priority (e.g., preferring standard paths like .claude/skills/) and deterministic tie-breakers2.
### 2\. How to Trace the True Original Source Repository

Because the dataset retains **every occurrence** of a skill across all repositories sharing the same `file_sha`[1][2], you can identify the earliest recorded version by comparing timestamps across all copies:

#### **Method 1: Using** **first\_commit\_at** **(Most Accurate)**

For skills where history was fetched (`history_fetched = 1`), the `artifacts` table records the **first\_commit\_at** timestamp[3][4]. By querying all occurrences sharing a specific `file_sha` and ordering by `first_commit_at ASC`, the top row reveals the earliest commit date and its hosting repository[3][4]:

```
SELECT 
    a.repo_full_name,
    a.path,
    a.first_commit_at,
    a.first_commit_author,
    a.dedup_primary
FROM artifacts a
WHERE a.file_sha = '
```
---
### 3\. Key Caveats &amp; Limitations

When tracing origin repositories, keep these dataset nuances in mind:

* **File Renames**: `first_commit_at` records the first commit of the file **at its current path only**[6]. If a developer renamed or moved a `SKILL.md` file within a repository, `first_commit_at` dates the move rather than its original creation[6].
* **Sampled History**: Commit history was fetched for all skills in standard locations plus a size-stratified sample of others (`history_fetched = 1`)[4][6].
* **Deleted or Private Repositories**: The dataset reflects public GitHub default branches as of July 2026[6][7]. If a skill originated in a private, deleted, or unindexed repository, the dataset can only trace the earliest *surviving public copy*.

---
References:
1. **Source Title 1** - GitHub - giuseppedestefanis/gitskills-sample | repos, artifact_siblings & mining_runs tables
2. **Source Title 2** - GitSkills: A Dataset of Agent Skills on GitHub | Zenodo | Description
3. **Source Title 3** - Gitskills_Preprint.pdf | Internal Structure
4. **Source Title 4** - GitHub - giuseppedestefanis/gitskills-sample | Schema
5. **Source Title 5** - Gitskills_Preprint.pdf | Table # Records Content
6. **Source Title 6** - mvaccargiu/gitskills · Datasets at Hugging Face | Discovery
7. **Source Title 7** - GitHub - giuseppedestefanis/gitskills-sample | Example Queries
---

## Q7: What do below columns mean?

1. frontmatter\_valid
2. discovered\_at, history\_fetched, composition\_fetched
3. dedup\_primary
4. has\_scripts 5, has\_references
5. content\_sha\_ok
6. composition\_truncated

Here is a breakdown of what each of these columns means in the **artifacts** table of the GitSkills dataset:

---

### 1\. `frontmatter_valid`

* **Meaning**: A binary flag (`1` or `0`) indicating whether the `SKILL.md` file contains valid, parsable **YAML front matter**[1].
* **Details**: A value of `1` indicates that the YAML header at the top of the file (specifying properties like `name` and `description`) parsed cleanly without syntax errors[1]. A value of `0` means the front matter was malformed, missing, or predated the specification[2].

---

### 2\. `discovered_at`, `history_fetched`, and `composition_fetched`

These three columns serve as process and retrieval tracking flags for the collection pipeline:

* **discovered\_at**: The ISO-8601 UTC timestamp recording when the automated discovery stage first found and logged the `SKILL.md` file occurrence on GitHub[1].
* **history\_fetched**: A boolean bookkeeping flag (`1` or `0`) indicating whether the file's Git **commit history** was fetched[1][3]. Commit histories were fetched for all skills in standard canonical locations (such as `.claude/skills/`) plus a size-stratified sample of other paths[3][4].
* **composition\_fetched**: A boolean flag (`1` or `0`) showing whether the **sibling directory composition** (bundled scripts, references, and template files) was fetched for the skill folder[1][5].

---

### 3\. `dedup_primary`

* **Meaning**: A deduplication flag set to **1** **for the single representative file** selected for each distinct Git content hash (`file_sha`), and **0** **for all duplicate copies**[1][6].
* **Details**: To avoid storing redundant data, the pipeline grouped identical files by content hash and selected one representative row (`dedup_primary = 1`) to carry full text, parsed front matter, folder composition, and sampled commit history[1][4]. All other occurrences remain in the table with `dedup_primary = 0` to track copying across repositories[4][7].

---

### 4\. `has_scripts`

* **Meaning**: A folder composition summary flag (`1` or `0`) indicating whether the representative skill folder contains **executable script files or directories** (such as a `scripts/` folder)[1][8].

---

### 5\. `has_references`

* **Meaning**: A folder composition summary flag (`1` or `0`) indicating whether the representative skill folder contains **reference files or documentation directories** (such as a `references/` folder)[1][8].

---

### 6\. `content_sha_ok`

* **Meaning**: A content-integrity verification flag recorded during raw text retrieval[1]:
  * **1**: The downloaded bytes were hashed and matched the original Git blob SHA (`file_sha`) exactly[1].
  * **2**: The content hash initially mismatched (e.g., due to line-ending changes) but was successfully repaired via GitHub's Git Blob API[1].
  * **0**: The file changed or was removed on GitHub between discovery and content fetching, so content could not be retrieved[5].

---

### 7\. `composition_truncated`

* **Meaning**: A folder-listing bookkeeping flag (`1` or `0`) set to `1` when a skill directory contained more files than the pipeline's **listing size cap**[1][9]. This alerts researchers that the corresponding entries in `artifact_siblings` represent a partial listing rather than an exhaustive index of the folder[1][9].

## Q8: Is there any identifier to in artifacts table that identifies if a commit was done by agent or human?
Yes, the **artifacts** table includes specific columns and metadata flags that help distinguish commits made by automated bots/agents versus human users:

---

### 1\. `first_commit_author_type` &amp; `last_commit_author_type`

These columns record the account classification assigned by GitHub for the commit's author[1]:

* **Bot**: Directly identifies commits authored by automated bot or agent accounts (e.g., GitHub Action bots, CI/CD tools, or dedicated bot users)[1].
* **User**: Identifies commits authored by standard user accounts[1].
* **Organization**: Identifies commits linked to organizational accounts[1].
* *(Empty / NULL)*: Indicates commits that lack a linked GitHub user account[1].

---

### 2\. Preserved Bot Logins in `first_commit_author` &amp; `last_commit_author`

* **Bot Account Logins**: Accounts flagged as `Bot` **retain their actual GitHub login handle** (e.g., `github-actions[bot]`) rather than being anonymized[1][2].
* **Human User Logins**: Accounts flagged as `User` are masked with a keyed, one-way anonymized hash code to preserve privacy while allowing cross-repository tracking[1][2].

---

### 3\. Preserved AI Assistant Names in Commit Messages

* While commit messages in `first_commit_message` and `last_commit_message` have email addresses and personal names redacted, **names of AI assistants in** **Co-authored-by** **trailer lines are explicitly preserved**[1][2].

---

### 💡 Key Nuance &amp; Research Limitation

While `author_type = 'Bot'` explicitly isolates dedicated bot accounts, **if a developer uses an AI agent via their local terminal or CLI tool (e.g., Claude Code or Cursor) under their personal Git config, the commit will be recorded with** **author\_type = 'User'**[1].

For this reason, quantifying how many skills are created or maintained by AI agents versus human developers remains an active empirical research question highlighted in the dataset's challenge paper[3][4].

## Q9: Is revision history of the skill file or just first and last commit? A skill file could have been copied and later that file could have been updated with additional skills. So initially the sha will be same but as commit history moves the file could have new sha. Is there any way to identify this link?
### 1\. Revision History: Summary Fields vs. Full Commit Logs

The dataset **does not contain the full commit-by-commit log** for every revision of a file[1][2]. Instead, it provides **summary commit history metrics** in the `artifacts` table for a subset of 458,548 skills (covering standard locations like `.claude/skills/` plus a size-stratified sample of other paths)[3]:

* **First and Last Commit Timestamps**: `first_commit_at` and `last_commit_at`[1].
* **Commit Count**: `commit_count` records the total number of commits touching the file[1].
* **Anonymized Authors**: `first_commit_author` and `last_commit_author` store stable, keyed anonymized codes (bot account logins are preserved)[1][6].
* **Account Types**: `first_commit_author_type` and `last_commit_author_type` (`User`, `Bot`, `Organization`, or empty)[1].
* **Masked Messages**: `first_commit_message` and `last_commit_message` (with personal names and emails redacted, but AI assistant trailers preserved)[1][6].

&gt; **Path Constraint**: The commit history follows the file at its **current path only**[2]. If a developer renamed or moved a `SKILL.md` file within a repository, `first_commit_at` dates the move and `commit_count` counts commits at that specific path[2].

---

### 2\. Linking Updated Skills When `file_sha` Changes

Because Git content hashes (`file_sha`) are calculated byte-for-byte, any addition or modification to a skill file results in a **new** **file\_sha**[1][7]. Because the dataset represents a point-in-time snapshot (July 2026), it does not store an explicit lineage graph linking modified SHAs[2][8].

However, you can trace and identify links between modified versions using these strategies:

#### **A. Inferred Lineage Within the Dataset**

1. **Front Matter Metadata Matching**: Skills carry structured YAML front matter (`name` and `description`)[1]. Grouping files by `name` across different `file_sha` records allows you to find modified versions or forks of the same skill[1][9].
2. **Fuzzy Text &amp; Clone Matching**: Since 50.5% of files in the dataset are verbatim copies and many others are slight variations, you can perform text similarity matching (such as Levenshtein distance, n-grams, or embeddings) on the `content` column to identify modified descendants[9].
3. **Cross-Repository Author Tracking**: Author accounts are anonymized using a consistent hash throughout the dataset[6][12]. If the same developer updates or copies a skill across different repositories, `first_commit_author` and `last_commit_author` remain identical, enabling author-based tracking[6][12].
4. **Temporal Ordering**: Comparing `first_commit_at` and `last_commit_at` across matched skills lets you establish chronological sequence (which version appeared first and which updated version followed)[1][2].

#### **B. External Mining (Bring Your Own Data)**

The MSR Mining Challenge explicitly supports a **"Bring Your Own Data" (BYOD)** policy[13]. Because `artifacts` contains `repo_full_name` and file `path` values[1], researchers can query the GitHub REST/GraphQL APIs or run local `git log -p` commands on cloned repositories to extract full revision histories, commit trees, and exact line-by-line diffs as files evolve over time[13].

## Q10: Is there any information about skills/subsets of skills that are reused? Any quality indicator implying from the number of copies made? Hot skills? Subset of skills divided and shared among multiple repositories?
### 1\. Reused Skills and Reuse Distribution

* **High Level of Duplication**: Exactly **50.5% of all file occurrences** in the GitSkills dataset are verbatim copies of another file[1]. While there are **3,797,117 total** **SKILL.md** **occurrences**, Git content hashing (`file_sha`) compresses them into **1,877,981 distinct skill contents**[1].
* **Distribution of Reuse**: The dataset exhibits a **heavy-tailed distribution** consisting of a long tail of singletons (skills appearing in only one project) and a prominent "head" of mass-copied contents that appear across hundreds of repositories[1].
* **Mechanism of Sharing**: Because agent skills lack a central package manager or registry, reuse occurs primarily through direct folder copying, scaffolding tools, catalogs, or template repositories[2].

---

### 2\. Copy Count as a Quality Indicator &amp; "Hot Skills"

* **Copy Count as a Quality Proxy**: The dataset creators propose **copy count** (the number of verbatim repository copies sharing the same `file_sha`) as a primary proxy for **skill quality, utility, and popularity ("hot skills")**[8][9].
* **Empirical Research Question**: The paper highlights validating copy count against quality metrics (e.g., readability, description effectiveness, and edit history) as an open research question for the MSR Challenge[8][9].
* **Querying Hot Skills**: The dataset provides sample queries to isolate top-copied skills by grouping by `file_sha` and ordering by copy count[10]:

```
SELECT MAX(name) AS name, COUNT(*) AS copies
FROM artifacts
GROUP BY file_sha
ORDER BY copies DESC LIMIT 10;

```

* **Security &amp; Quality Trade-offs**: While high copy counts signal popular "hot skills," the authors note that widely copied skills present risks: outdated instructions or modified copies that introduce unauthorized command/network execution (a supply-chain risk without formal package registries)[11][12].

---

### 3\. Subsets of Skills Divided and Shared Across Repositories

* **Categorization &amp; Location**: Skills often exist as part of broader suites or subdirectories:
  * **55.4%** sit under a general `skills/` directory[1].
  * **9.8%** sit in canonical paths like `.claude/skills/

## Q11: Can the skill.md file give any information of its creation - agent, tool, official skills from frontier model creators, templates widely shared or promoted, bots or shared collaboration with user &amp; LLM?
A `SKILL.md` file and its surrounding directory reveal rich information about how the skill was created, which tools or frontier models it targets, whether it originated from official creators, and whether human-LLM collaboration or automated bots were involved[1].

---

### 1\. Identifying Frontier Model Creators &amp; Official Vendor Skills

* **Official Repositories**: Frontier model creators like Anthropic published the initial open specification in October 2025 and released official, reference skills in public repositories (e.g., Anthropic's official skills repository containing skills like `web-artifacts-builder`)[2].
* **Directory Path Conventions**: The file path often indicates whether a skill was written for a specific vendor's CLI agent or a vendor-neutral setup[6]:
  * **Canonical / Tool-Specific Paths**: Paths like `.claude/skills/
  * **Vendor-Neutral / General Paths**: Files located under generic `skills/` directories or elsewhere in the repository indicate open-specification or multi-agent usage
### 2\. Identifying Tools, Agents, and Runtimes Inside the File &amp; Folder

* **Front Matter &amp; Instruction Body**: The YAML header (`name`, `description`) and Markdown body frequently mention specific tools, CLI environment variables (e.g., `CLAUDE_CMD`), API configurations, or model specifications (e.g., OpenRouter, `grok-4-1-fast-reasoning`, or `kimi-k2.5`)[1].
* **Bundled Metadata &amp; Registry Files**: Sibling files inside the skill folder often reveal specific tooling, package managers, or registries used to generate or install the skill[9][12]:
  * **Registry Footprints**: Files like `.clawhub/origin.json` or `_meta.json` log publishing origins, slug names, and versions from community hubs[9][13].
  * **Build and Script Manifests**: Associated files like `pyproject.toml`, bash scripts (`scripts/`), or schema files explicitly document author emails, requirements, and execution tools[12].

### 3\. Detecting Creation by Bots vs. Human–LLM Collaboration

* **Commit Author Types**: In the dataset's commit history, commits are classified by account type (`User`, `Bot`, or `Organization`)[1]. Bot accounts retain their actual GitHub handle (e.g., `github-actions[bot]`), signaling automated creation or deployment[1].
* **AI Assistant Commit Trailers**: While human author names and emails are masked for privacy, **names of AI assistants in** **Co-authored-by** **commit message trailers are explicitly preserved** (e.g., `Co-authored-by: Claude &lt;...&gt;`)[3][16]. This provides direct commit-level evidence of human-LLM co-creation[3][16].
* **Linguistic &amp; Structural Cues**: LLM-generated or agent-maintained skills can be identified by shrinking semantic diversity, formulaic template structures, or standardized metaprompts embedded directly in the instruction text[17].

### 4\. Widely Shared Templates &amp; Mass-Copied Skills

* **Verbatim Copying**: Because agent skills lack a central package manager, **50.5% of all** **SKILL.md** **files in the dataset are byte-for-byte verbatim copies** of another file[7][20].
* **Scaffolding &amp; Boilerplate**: Highly copied skills (identifiable by high copy counts for a single content hash `file_sha`) point directly to widely promoted project templates, community catalogs, or framework scaffolding tools that auto-generate skill files when initializing new projects[20].

## Q12: How did the authors test data sanctity? How did they handle when github APIs hit rate limit or any network error caused improper repo information. Trying to figure how to eliminate records that have data quality issues
To ensure data sanctity and build a reliable dataset despite the scale and unpredictability of GitHub's network APIs, the creators of **GitSkills** implemented several automated validation steps, fallback repair mechanisms, and explicit tracking flags[1].

---

### 1\. How the Authors Tested and Verified Data Sanctity

1. **Byte-for-Byte Git Hash Verification (** **file\_sha** **)**:
  * Every file in Git is identified by a deterministic Git blob SHA[1][2]. When raw file content was fetched from GitHub, the pipeline re-hashed the downloaded bytes and verified them against the expected Git SHA[1].
  * The status was recorded in the **content\_sha\_ok** column[1]:
    * **1**: Perfect match; downloaded bytes matched the Git blob SHA[1].
    * **2**: Initial hash mismatch (e.g., due to raw CDN line-ending normalization issues), but successfully repaired and verified by re-fetching via GitHub’s REST Git Blob API[1].
    * **0**: Unrecoverable mismatch; file changed or was deleted from GitHub between the initial discovery scan and content retrieval (affecting 0.9% of distinct contents)[1].
2. **YAML Front Matter Validation (** **frontmatter\_valid** **)**:
  * The YAML front matter of every fetched `SKILL.md` was parsed against front-matter constraints[1][6].
  * Files with valid, parsable YAML headers received `frontmatter_valid = 1`, while malformed, syntax-error-ridden, or legacy pre-format headers were flagged with `0`[1][7].
3. **Anonymization &amp; Privacy Validation**:
  * Before release, the database underwent automated scans to verify that all email addresses and personal names in commit messages and repository descriptions were redacted, confirming no residual PII remained[4][5].

---

### 2\. How GitHub API Rate Limits and Network Errors Were Handled

Because the collection pipeline made millions of requests across GitHub’s REST, GraphQL, Code Search APIs, and raw CDN, network disruptions and API rate limits were handled systematically[2]:

* **Database Checkpointing**: The pipeline checkpointed its execution state directly in the SQLite database after every batch, enabling automatic resumption without data corruption or duplicate fetches if a network error or rate limit interrupted a run[3].
* **Provenance Logging (** **mining\_runs** **)**: The `mining_runs` table tracks every execution attempt[8][9]. Four collection runs that crashed due to pipeline interruptions were closed retroactively with a zero count to preserve complete audit logs[8].
* **API Fallbacks**: When CDN fetching failed or returned altered SHAs, the pipeline automatically fell back to GitHub's official REST Git Blob API to recover exact bytes[1].
* **Explicit Fetch Tracking Flags**: Rather than silently ignoring failures, the dataset explicitly logs whether metadata calls succeeded:
  * **repos.metadata\_fetched**: Indicates whether repository-level attributes (stars, owner, license) were successfully retrieved[9].
  * **artifacts.history\_fetched**: Tracks whether commit history retrieval succeeded[1]. (The authors note commit history failed for 1,166 standard-location skills)[4].
  * **artifacts.composition\_fetched**: Tracks whether sibling directory listing succeeded[1]. (Folder listings failed for 1,544 representative skills)[4].

---

### 3\. How to Eliminate Records with Data Quality Issues

If you want to filter out incomplete, corrupt, non-standard, or unverified records for your analysis, you can use the dataset's built-in quality flags:

#### **A. Filter for High-Quality Primary Skills**

To analyze only verified, non-duplicate skill files with valid front matter and intact content, apply this standard SQL filter:

```
SELECT *
FROM artifacts
WHERE dedup_primary = 1          -- Primary representative file for the content group [1]
  AND content_sha_ok IN (1, 2)   -- Content verified byte-for-byte against Git SHA [1]
  AND content IS NOT NULL         -- Content text successfully retrieved [1]
  AND frontmatter_valid = 1      -- Parsed valid YAML header [1]
  AND filename = 'SKILL.md'      -- Strict case match (excludes legacy lowercase or generic filenames) [1, 7]
  AND location_class IN ('canonical', 'skills-dir'); -- Standard skill directory paths [1, 7]

```

#### **B. Filter for Repositories with Complete Metadata**

To exclude skills whose hosting repositories hit API fetching errors:

```
SELECT a.*, r.stars, r.language, r.license
FROM artifacts a
JOIN repos r ON a.repo_full_name = r.full_name
WHERE r.metadata_fetched = 1;     -- Ensures repository attributes fetched successfully [9]

```

#### **C. Filter for Complete Folder Listings &amp; Commit History**

When analyzing bundled sibling scripts or commit histories, exclude records where history or composition listing was skipped, failed, or capped:

```
SELECT *
FROM artifacts
WHERE history_fetched = 1        -- Commit history successfully fetched [1]
  AND composition_fetched = 1    -- Directory contents successfully fetched [1]
  AND composition_truncated = 0; -- Excludes massive folders that exceeded the l
```

