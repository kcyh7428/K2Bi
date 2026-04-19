"""Scripts package marker so `python3 -m scripts.lib.<module>` works.

Production entry points (deploy_config.py, minimax_review.py) still run as
standalone executables via their shebangs; the package layout only matters
for `-m` invocation and for `from scripts.lib import ...` in tests + hooks.
"""
