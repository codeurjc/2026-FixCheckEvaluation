"""
Fast, deterministic unit tests for FixGenerator's SEARCH/REPLACE → diff
pipeline and the diff normalizer. No Docker, Ollama or network required.

Run with:

    .venv/bin/python -m pytest test/test_fixgenerator_units.py -v
"""

from FixGenerator import (
    build_diff_from_blocks,
    normalize_diff,
    parse_search_replace_blocks,
)

SOURCE = """\
class Foo {
    int add(int a, int b) {
        return a + b;
    }
}
"""


# ------------------------------------------------------- parse_search_replace

def test_parse_single_block():
    response = """\
src/Foo.java
<<<<<<< SEARCH
        return a + b;
=======
        return a - b;
>>>>>>> REPLACE
"""
    blocks = parse_search_replace_blocks(response)
    assert len(blocks) == 1
    path, search, replace = blocks[0]
    assert path == "src/Foo.java"
    assert search == "        return a + b;"
    assert replace == "        return a - b;"


def test_parse_ignores_code_fences_for_path():
    response = """\
src/Foo.java
```java
<<<<<<< SEARCH
        return a + b;
=======
        return a - b;
>>>>>>> REPLACE
```
"""
    blocks = parse_search_replace_blocks(response)
    assert len(blocks) == 1
    assert blocks[0][0] == "src/Foo.java"


def test_parse_multiple_blocks():
    response = """\
a.java
<<<<<<< SEARCH
x
=======
y
>>>>>>> REPLACE
b.java
<<<<<<< SEARCH
p
=======
q
>>>>>>> REPLACE
"""
    blocks = parse_search_replace_blocks(response)
    assert [b[0] for b in blocks] == ["a.java", "b.java"]


# -------------------------------------------------------- build_diff_from_blocks

def test_build_diff_applies_and_is_wellformed():
    response = """\
src/Foo.java
<<<<<<< SEARCH
        return a + b;
=======
        return a - b;
>>>>>>> REPLACE
"""
    blocks = parse_search_replace_blocks(response)
    diff, applied, failed = build_diff_from_blocks([("src/Foo.java", SOURCE)], blocks)
    assert applied == 1
    assert failed == []
    assert "--- a/src/Foo.java" in diff
    assert "+++ b/src/Foo.java" in diff
    assert "@@" in diff
    assert "-        return a + b;" in diff
    assert "+        return a - b;" in diff


def test_build_diff_is_whitespace_tolerant():
    # SEARCH uses different indentation/spacing than the real source, yet the
    # block should still be located.
    response = """\
src/Foo.java
<<<<<<< SEARCH
  return a+b;
=======
  return a * b;
>>>>>>> REPLACE
"""
    blocks = parse_search_replace_blocks(response)
    diff, applied, failed = build_diff_from_blocks([("src/Foo.java", SOURCE)], blocks)
    assert applied == 1
    assert failed == []
    assert "+  return a * b;" in diff


def test_build_diff_reports_unlocatable_block():
    response = """\
src/Foo.java
<<<<<<< SEARCH
        this line is not in the file at all;
=======
        replacement;
>>>>>>> REPLACE
"""
    blocks = parse_search_replace_blocks(response)
    diff, applied, failed = build_diff_from_blocks([("src/Foo.java", SOURCE)], blocks)
    assert applied == 0
    assert diff == ""
    assert len(failed) == 1


def test_build_diff_resolves_path_by_basename():
    # Model names the file with a wrong parent path; basename still resolves it.
    response = """\
wrong/dir/Foo.java
<<<<<<< SEARCH
        return a + b;
=======
        return a - b;
>>>>>>> REPLACE
"""
    blocks = parse_search_replace_blocks(response)
    diff, applied, failed = build_diff_from_blocks(
        [("src/main/Foo.java", SOURCE)], blocks
    )
    assert applied == 1
    assert "--- a/src/main/Foo.java" in diff


# ------------------------------------------------------------- normalize_diff

def test_normalize_recomputes_hunk_counts():
    # Header claims wrong counts; normalize_diff must recompute them.
    diff = (
        "--- a/f.txt\n"
        "+++ b/f.txt\n"
        "@@ -1,3 +1,9 @@\n"
        " a\n"
        " b\n"
        "-c\n"
        "+C\n"
        "+X\n"
        " d"
    )
    out = normalize_diff(diff)
    assert "@@ -1,4 +1,5 @@" in out


def test_normalize_strips_trailing_garbage():
    diff = (
        "--- a/f.txt\n"
        "+++ b/f.txt\n"
        "@@ -1,2 +1,2 @@\n"
        " a\n"
        "-b\n"
        "+B\n"
        "*** End of File ***\n"
    )
    out = normalize_diff(diff)
    assert "End of File" not in out


def test_normalize_fixes_blank_context_lines():
    # A blank context line inside the hunk lost its leading space.
    diff = "--- a/f.txt\n+++ b/f.txt\n@@ -1,3 +1,3 @@\n a\n\n-b\n+B"
    out = normalize_diff(diff)
    lines = out.split("\n")
    # The blank line is rewritten as a single-space context line, so no line in
    # the hunk body is truly empty.
    assert " " in lines
    assert "" not in lines
