"""Scope keying, context resolution, masking, refuse-guard (DESIGN §5/§6/§8).

Pure logic: context resolution (longest-prefix), scope computation, subsumption,
refuse-guard, cwd denylist, ``exclude`` masking devices.

Implemented in T6 (resolution/scope/guards) and T7 (masking/whitelist).
"""
