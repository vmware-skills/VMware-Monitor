"""Verified endpoint/attribute spec data for vmware-monitor.

Seeded from the family's cross-checked VCF 9.1 endpoint spec (section D) to guard
踩坑 #36 (a sibling skill once shipped hallucinated REST endpoints, half of which
404'd). Nothing here is invented: every entry was verified against Broadcom
OpenAPI / pyVmomi type metadata before it was written down. Regression tests
assert this skill's ops code only ever touches paths and attributes listed here.
"""
