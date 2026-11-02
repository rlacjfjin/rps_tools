"""rps_solver experiment analyzer.

Given a baseline + modified pair of input/output JSONs from rps_solver,
produces a structured report explaining the OBJ difference: KPIs, OBJ
contribution diff, chain trace, mermaid graph, block activation diff,
shadow prices, block-level attribution, critical path, property limit
binding analysis, cycle detection and balance validation.

Entry point:
    python -m rps_tools.exp_analyzer <experiment_dir>

See sample_patch.json for the experiment descriptor schema.
"""
