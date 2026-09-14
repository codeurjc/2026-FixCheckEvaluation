"""
d4j/developer_fix.py — a Defects4J bug's developer fix, as a diff.

The developer's fix is the reference a patch-correctness check is calibrated
against: FixCheck should not flag it. It lives here because it knows about the
benchmark (a bug's buggy ``<id>b`` and fixed ``<id>f`` revisions), not about
any tool; the FixCheck replay and the end-to-end tests share this one copy.
"""

import difflib


def developer_diff(buggy_sources, fixed_sources):
    """Build a unified diff from the buggy sources to the developer's fix.

    ``buggy_sources`` and ``fixed_sources`` are ``(relative_path, content)``
    pairs read from the ``<id>b`` and ``<id>f`` checkouts, the fixed ones at
    the same relative paths. Files the fix leaves unchanged contribute nothing,
    so an empty string means the two revisions do not differ in those files.
    """
    fixed_by_path = dict(fixed_sources)
    diffs = []
    for rel_path, buggy_content in buggy_sources:
        fixed_content = fixed_by_path.get(rel_path, buggy_content)
        if fixed_content == buggy_content:
            continue
        diff = difflib.unified_diff(
            buggy_content.split("\n"), fixed_content.split("\n"),
            fromfile=f"a/{rel_path}", tofile=f"b/{rel_path}", lineterm="",
        )
        diffs.append("\n".join(diff))
    return "\n".join(d for d in diffs if d)
