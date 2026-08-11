"""
d4j — tools that know about the Defects4J benchmark itself, as opposed to the
experiment pipeline that runs on top of it.

- :mod:`d4j.defects4j_bugs` — which bugs exist, which are deprecated, where
  each one's issue report lives, and how to read a cached issue.
- :mod:`d4j.fetch_issues` — downloads every bug's issue report once into
  ``d4j/issues/``, so a campaign never depends on a tracker's API being up or
  within its rate limit.
"""
