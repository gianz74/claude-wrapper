"""Config loading + validation (DESIGN §7).

Loads ``~/.config/claude-wrapper/config.toml`` via ``tomllib`` and models
``[setup]``, ``[reaper]``, ``[[mounts]]`` and ``[[contexts]]``.

Implemented in T2.
"""
