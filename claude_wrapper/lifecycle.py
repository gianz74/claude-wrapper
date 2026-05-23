"""Container lifecycle (DESIGN §4/§10): the 3-tier CoW hierarchy.

``build_base`` (T4), ``build_templates`` (T5), ``run`` + stamp drift (T8),
reaper/gc/delete (T10).

Implemented across T4, T5, T8, T10.
"""
