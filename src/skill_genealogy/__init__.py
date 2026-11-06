"""How skill files spread from registry repos, and how copies change after that.

Run a stage with ``python -m skill_genealogy.<module> [--dev] [--force]``.
``--dev`` writes the small registry sample. ``--force`` rebuilds outputs
that already exist. Stages 3 and 6 download from GitHub. The others do not.

Pipeline (same order as ``docs/skill_genealogy/STAGE_BY_STAGE_PLAN.md``):

    dataset (artifacts, repos)   wild_history   s2 MinHash + clusters
              |                       |                 |
              v                       |                 |
      1 check_data  ------------------+-----------------+
              |
      2 select_registries
              |
      3 download_registry_history          [network]
              |
      4 build_registry_lineages
              |
      5 find_copies
              |
      6 download_copied_skill_history      [network]
              |
      7 assign_families
              |
        +-----+-------------------+----------------------+
        v                         v                      v
      8 build_family_trees     9 measure_copy_changes 10 measure_staleness
        +-----+-------------------+----------------------+
              v
     11 summarize_results
              |
     12 export_tree_viewer
              |
     13 export_staleness_viewer
              |
     14 repo_report + export_repo_explorer

Commands, in that order:

1. ``python -m skill_genealogy.check_data``
2. ``python -m skill_genealogy.select_registries``
3. ``python -m skill_genealogy.download_registry_history``
4. ``python -m skill_genealogy.build_registry_lineages``
5. ``python -m skill_genealogy.find_copies``
6. ``python -m skill_genealogy.download_copied_skill_history``
7. ``python -m skill_genealogy.assign_families``
8. ``python -m skill_genealogy.build_family_trees``
9. ``python -m skill_genealogy.measure_copy_changes``
10. ``python -m skill_genealogy.measure_staleness``
11. ``python -m skill_genealogy.summarize_results``
12. ``python -m skill_genealogy.export_tree_viewer``
13. ``python -m skill_genealogy.export_staleness_viewer``
14. ``python -m skill_genealogy.repo_report``
    ``python -m skill_genealogy.export_repo_explorer``

Each command also accepts ``--dev`` and ``--force``.
"""
