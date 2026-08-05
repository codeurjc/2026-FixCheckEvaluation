"""
FixCheckWrapper.py — runs the vendored FixCheck overfitting check
(github.com/facumolina/fixcheck) against an already-patched Defects4J
checkout.

Only meaningful once a candidate patch is applied and every trigger test
passes: FixCheck starts from the bug-revealing trigger test(s), generates
small input variations ("prefixes"), runs them against the patched program,
and flags the ones that still fail the same way as the original bug as
evidence the patch is overfitting rather than genuinely correct.

Like ``FixGenerator.py``, this module is kept apart from ``Experiment.py`` so
it can be exercised and unit tested independently of the LLM fix-generation
pipeline (see ``test/unit/test_fixcheck_units.py`` and
``test/e2e/test_fixcheck_devfix.py``). Unlike ``FixGenerator``, it still needs
a running Defects4J Docker container and a shared-volume ``workdir`` — the
things it wraps (compiling, exporting classpaths, invoking the FixCheck jar)
only exist inside that container — but it has no dependency on
``Experiment.py`` itself, does not touch LLM generation, and takes its
configuration directly through ``__init__`` rather than an argparse
``Namespace``.
"""

import csv
import os
import re

from docker_utils import exec_in_container, export_property, run_step

HERE = os.path.dirname(os.path.abspath(__file__))
FIXCHECK_DIR = os.path.join(HERE, "fixcheck")
FIXCHECK_JAR = os.path.join(FIXCHECK_DIR, "build", "libs", "fixcheck-all-1.0.0.jar")

# Option keys accepted by FixCheck's `assertion-generator` property (see
# fixcheck/src/main/java/org/imdea/fixcheck/properties/AssertionGeneratorProperty.java).
FIXCHECK_ASSERTION_GENERATORS = [
    "assert-true", "previous-assertion", "replit-code-llm", "gpt-3.5",
    "codellama", "llama3.1",
]
DEFAULT_FIXCHECK_PREFIXES = 25
DEFAULT_FIXCHECK_ASSERTIONS = "previous-assertion"
DEFAULT_FIXCHECK_SIMILARITY_THRESHOLD = 0.8

# Method names FixCheck treats as assertions and therefore refuses to mutate
# (``transform/input/InputTransformer.java``'s ``isAssertion``).
FIXCHECK_ASSERTION_CALLS = (
    "assertNotNull", "assertTrue", "assertFalse", "assertEquals",
    "assertNotEquals", "fail", "check",
)
_ASSERTION_STMT_RE = re.compile(rf"^\s*(?:{'|'.join(FIXCHECK_ASSERTION_CALLS)})\s*\(")

# Ordered by preference when several literal types are equally frequent.
FIXCHECK_INPUT_TYPE_PRIORITY = ["java.lang.String", "int", "double", "long", "boolean"]


def group_triggers_by_class(trigger_tests):
    """Group ``"FQCN::method"`` trigger tests by their class.

    FixCheck handles one test class per run (see ``FixCheckWrapper.run``), so
    its per-class fan-out needs the trigger tests grouped this way rather
    than as a flat list.

    Returns a dict mapping each FQCN to the list of its trigger method names,
    in order of first appearance; both the class order and each class's
    method order follow ``trigger_tests``, and duplicate ``"FQCN::method"``
    entries contribute their method only once.
    """
    grouped = {}
    for trigger in trigger_tests:
        cls, _, method = trigger.partition("::")
        methods = grouped.setdefault(cls, [])
        if method not in methods:
            methods.append(method)
    return grouped


def fixcheck_failure_log_path(workdir, fqcn):
    """Path of a trigger class's clean (header-free) pre-fix failure trace.

    Shared by :func:`write_fixcheck_failure_logs` (writer, pre-fix) and
    :meth:`FixCheckWrapper.run` (reader, post-fix) so the two never drift
    apart.
    """
    return os.path.join(workdir, ".fixcheck", f"{fqcn}.failing_tests")


def write_fixcheck_failure_logs(workdir, trigger_tests, trigger_raw):
    """Write one clean pre-fix failure-trace file per trigger class.

    FixCheck needs the *original* (pre-patch) failure trace, but by the time
    it runs the patch has already been applied and Defects4J's
    ``failing_tests`` file has been overwritten. This captures it early
    (before fix generation, from ``Experiment.py``'s pipeline) as
    ``fixcheck_failure_log_path(workdir, fqcn)`` — the concatenated raw
    ``failing_tests`` content of that class's trigger method(s), with no
    ``$ cmd`` header, since ``FixCheckProperties.loadFailureLog()`` reads the
    file as-is.

    Args:
        workdir: The bug's checkout directory (shared host/container path).
        trigger_tests: List of ``"FQCN::method"`` strings.
        trigger_raw: The dict returned by ``Experiment.run_trigger_tests_raw``.

    Returns:
        Dict mapping each trigger FQCN to the path written.
    """
    os.makedirs(os.path.join(workdir, ".fixcheck"), exist_ok=True)
    paths = {}
    for fqcn, methods in group_triggers_by_class(trigger_tests).items():
        content = "\n".join(
            trigger_raw.get(f"{fqcn}::{method}", ("", ""))[1] for method in methods
        )
        path = fixcheck_failure_log_path(workdir, fqcn)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        paths[fqcn] = path
    return paths


def _strip_java_comments(source):
    """Remove ``//`` and ``/* */`` comments, preserving string/char literals.

    Comments have to go before statements are split: a comment sitting above
    an assertion (``// Leading zero tests`` in Lang 1's ``TestLang747``) would
    otherwise become part of that statement's text, stop it from matching
    :data:`_ASSERTION_STMT_RE`, and smuggle the assertion's literals back into
    the mutable set.
    """
    out = []
    i = 0
    n = len(source)
    quote = None
    while i < n:
        ch = source[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(source[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and source[i + 1] == "/":
            while i < n and source[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and source[i + 1] == "*":
            i += 2
            while i + 1 < n and not (source[i] == "*" and source[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _split_java_statements(source):
    """Split Java source into top-level statements.

    Scans character by character so that a ``;`` inside a string/char literal
    or nested parentheses does not split a statement, and so that a block
    statement ends at its own closing brace. Expects comment-free input (see
    :func:`_strip_java_comments`). Good enough for JUnit test bodies, which is
    all this is used for.
    """
    statements = []
    current = []
    depth = 0
    quote = None
    i = 0
    while i < len(source):
        ch = source[i]
        current.append(ch)
        if quote:
            if ch == "\\":
                if i + 1 < len(source):
                    current.append(source[i + 1])
                    i += 1
            elif ch == quote:
                quote = None
        elif ch in ('"', "'"):
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            # A block statement (if/for/while/try) ends at its closing brace,
            # with no trailing ';'. Without this it would be glued to the
            # statement that follows, dragging that statement's literals in
            # even when it is an assertion we meant to exclude.
            if ch == "}" and depth == 0:
                statements.append("".join(current))
                current = []
        elif ch == ";" and depth == 0:
            statements.append("".join(current))
            current = []
        i += 1
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def _mutable_statements(test_method_source):
    """Return the part of a test method FixCheck is willing to mutate.

    ``InputTransformer.getRandomInputKnownType`` collects candidate literals
    only from statements that are neither blocks nor assertions, so literals
    that appear exclusively inside ``assertEquals(...)`` & co. are invisible
    to it. Counting those would make :func:`select_fixcheck_inputs` propose
    a type FixCheck then cannot find, which it reports by throwing
    ``IllegalArgumentException: No locals of type <T>`` and dying without
    writing a report.
    """
    body = _strip_java_comments(test_method_source)
    first_brace = body.find("{")
    last_brace = body.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        body = body[first_brace + 1:last_brace]
    kept = [s for s in _split_java_statements(body) if not _ASSERTION_STMT_RE.match(s)]
    return "\n".join(kept)


def count_java_literals(source):
    """Count Java literal occurrences per FixCheck ``inputs-class`` type."""
    string_re = re.compile(r'"(?:[^"\\]|\\.)*"')
    string_count = len(string_re.findall(source))
    # Blank out string literals (same length, so later spans aren't shifted)
    # before scanning for numeric/boolean literals, so digits inside a
    # string's *content* aren't also counted as int/double literals.
    without_strings = string_re.sub(lambda m: " " * len(m.group()), source)

    float_re = re.compile(r"\b\d+\.\d+[fFdD]?\b")
    double_count = len(float_re.findall(without_strings))
    without_floats = float_re.sub(lambda m: " " * len(m.group()), without_strings)

    int_re = re.compile(r"\b\d+([lL])?\b")
    int_suffixes = int_re.findall(without_floats)
    long_count = sum(1 for suffix in int_suffixes if suffix)

    return {
        "java.lang.String": string_count,
        "int": len(int_suffixes) - long_count,
        "double": double_count,
        "long": long_count,
        "boolean": len(re.findall(r"\btrue\b|\bfalse\b", without_strings)),
    }


def select_fixcheck_inputs(methods, method_sources):
    """Pick one ``inputs-class`` plus the trigger methods it can mutate.

    A FixCheck run takes a whole test class and *every* method listed in
    ``test-methods``, and generates variations for each in turn. If any one of
    them has no literal of ``inputs-class``, ``InputTransformer`` throws and
    the exception propagates out of ``FixCheck.main``, so the entire run dies
    and no report is written even for the methods that did work. Passing only
    the methods that can actually be mutated keeps one awkward trigger method
    from wasting the whole class.

    Returns ``(inputs_class, usable_methods)``, or ``(None, [])`` when no type
    works for any method.
    """
    counts = {
        m: count_java_literals(_mutable_statements(method_sources.get(m, "")))
        for m in methods
    }
    best = max(
        FIXCHECK_INPUT_TYPE_PRIORITY,
        key=lambda t: (
            sum(1 for m in methods if counts[m][t] > 0),
            -FIXCHECK_INPUT_TYPE_PRIORITY.index(t),
        ),
    )
    usable = [m for m in methods if counts[m][best] > 0]
    return (best, usable) if usable else (None, [])


def build_fixcheck_properties(test_classes_path, test_class, test_methods,
                               test_classes_src, failure_log_path, inputs_class,
                               num_prefixes, assertion_generator):
    """Render a FixCheck ``.properties`` file for one trigger test class.

    Key names mirror
    ``fixcheck/src/main/java/org/imdea/fixcheck/properties/FixCheckProperties.java``
    (``loadProperties()``) exactly. ``assertion_generator`` is one of the CLI
    option keys from ``AssertionGeneratorProperty.java`` (e.g.
    ``previous-assertion``), not the Java class name it resolves to.
    """
    lines = [
        f"test-classes-path={test_classes_path}",
        f"test-class={test_class}",
        f"test-methods={':'.join(test_methods)}",
        f"test-classes-src={test_classes_src}",
        f"test-failure-trace-log={failure_log_path}",
        f"inputs-class={inputs_class}",
        f"number-of-prefixes={num_prefixes}",
        f"assertion-generator={assertion_generator}",
    ]
    return "\n".join(lines) + "\n"


def parse_fixcheck_report(report_csv_text):
    """Parse FixCheck's one-row ``report.csv`` into a dict.

    The header is defined in ``writer/ReportWriter.java``. The report has no
    "non-compiling" column, so it is recovered as ``total - passing -
    crashing - assertion_failing`` (FixCheck's prefix buckets -- non-
    compiling, passing, crashing, assertion-failing -- are a partition of
    every generated prefix; see ``FixCheck.savePrefix()``), where ``total``
    is the report's ``output_prefixes`` column.

    Returns ``None`` when the content is empty, headerless, or its data row
    doesn't line up with its header (e.g. a partially-written file).
    """
    if not report_csv_text or not report_csv_text.strip():
        return None
    rows = [row for row in csv.reader(report_csv_text.splitlines()) if row]
    if len(rows) < 2:
        return None
    header, data = rows[0], rows[1]
    if len(header) != len(data):
        return None
    record = dict(zip(header, data))

    def as_int(key):
        try:
            return int(record[key])
        except (KeyError, ValueError):
            return None

    total = as_int("output_prefixes")
    passing = as_int("passing_prefixes")
    crashing = as_int("crashing_prefixes")
    assertion_failing = as_int("assertion_failing_prefixes")
    if None in (total, passing, crashing, assertion_failing):
        return None

    return {
        "test_class": record.get("test_class", ""),
        "input_prefixes": as_int("input_prefixes"),
        "inputs_class": record.get("inputs_class", ""),
        "target_class": record.get("target_class", ""),
        "prefixes_gen_time_ms": as_int("prefixes_gen_time"),
        "assertions_gen_time_ms": as_int("assertions_gen_time"),
        "prefixes_running_time_ms": as_int("prefixes_running_time"),
        "total": total,
        "passing": passing,
        "crashing": crashing,
        "assertion_failing": assertion_failing,
        "non_compiling": total - passing - crashing - assertion_failing,
    }


def parse_fixcheck_scores(scores_csv_text):
    """Parse FixCheck's ``scores-failing-tests.csv`` into similarity scores.

    Rows are ``<prefix-class-name>,<score>`` (score in ``[0, 1]``; see
    ``checker/FailureChecker.similarity()``). Lenient about an optional
    ``prefix,score`` header and blank lines; malformed rows are skipped
    rather than raising, since a partially-written file should still yield
    whatever scores it has.
    """
    scores = []
    if not scores_csv_text:
        return scores
    for row in csv.reader(scores_csv_text.splitlines()):
        if len(row) < 2:
            continue
        score_str = row[1].strip()
        if score_str.lower() == "score":
            continue  # header row
        try:
            scores.append(float(score_str))
        except ValueError:
            continue
    return scores


def _resolve_classpath(workdir, cp_string):
    """Make every ``:``-separated classpath entry absolute.

    Defects4J's ``cp.*`` exports may be workdir-relative; FixCheck runs with
    its CWD set to a per-class scratch directory (see ``FixCheckWrapper.run``),
    so a relative entry would resolve against the wrong directory unless
    anchored to ``workdir`` first. Already-absolute entries are left
    untouched.
    """
    entries = [e for e in cp_string.split(":") if e]
    resolved = [e if os.path.isabs(e) else os.path.join(workdir, e) for e in entries]
    return ":".join(resolved)


def _read_text_or_none(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _write_text(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content or "")


class FixCheckWrapper:
    """Runs FixCheck for every trigger test class of a patched checkout.

    Configuration is passed directly to ``__init__`` rather than through
    ``Experiment.py``'s argparse ``Namespace``, so a wrapper can be built and
    exercised on its own (see ``test/e2e/test_fixcheck_devfix.py``) without
    constructing a fake CLI-args object.
    """

    def __init__(self, num_prefixes=DEFAULT_FIXCHECK_PREFIXES,
                 assertion_generator=DEFAULT_FIXCHECK_ASSERTIONS,
                 similarity_threshold=DEFAULT_FIXCHECK_SIMILARITY_THRESHOLD,
                 inputs_class=None, jar_path=FIXCHECK_JAR):
        self.num_prefixes = num_prefixes
        self.assertion_generator = assertion_generator
        self.similarity_threshold = similarity_threshold
        self.inputs_class = inputs_class
        self.jar_path = jar_path

    def run(self, container, workdir, trigger_tests, trigger_method_sources):
        """Run FixCheck on an already-patched, already-plausible checkout.

        Only meaningful once the patch is applied and every trigger test
        passes (see ``Experiment.main()``'s ``triggers_fixed`` gate) --
        FixCheck starts from the bug-revealing test(s), generates small input
        variations, runs them against the patched program, and flags the
        ones that still fail the same way as the original bug as evidence
        the patch is overfitting rather than genuinely correct.

        One FixCheck run handles a single test class, so this fans out over
        every trigger class (``group_triggers_by_class``) and aggregates
        their per-class reports/scores. FixCheck is advisory: any exception,
        or a missing/unparsable report, is logged as a ``[fixcheck] WARNING``
        and folded into the returned dict's ``ok``/``error`` fields rather
        than raised, so it never aborts the experiment and never affects
        ``fixed``.

        Returns:
            A dict: ``ran``, ``ok``, ``assertion_generator``, ``num_prefixes``,
            ``similarity_threshold``, ``inputs_class`` (per FQCN),
            ``per_test_class`` (list of per-class records, see
            ``_run_for_class``), ``analyzed_test_classes`` (how many of
            those actually produced a report), ``failing_prefixes``
            (crashing + assertion-failing, summed over classes),
            ``max_failure_similarity`` (max score over classes) and
            ``suspicious`` (``failing_prefixes > 0 and
            max_failure_similarity >= self.similarity_threshold``).

            ``suspicious`` being false is only meaningful when
            ``analyzed_test_classes > 0``: with nothing analyzed there is
            simply no evidence either way, which is why the two are reported
            separately.
        """
        result = {
            "ran": True,
            "ok": True,
            "assertion_generator": self.assertion_generator,
            "num_prefixes": self.num_prefixes,
            "similarity_threshold": self.similarity_threshold,
            "inputs_class": {},
            "per_test_class": [],
            "analyzed_test_classes": 0,
            "failing_prefixes": 0,
            "max_failure_similarity": 0.0,
            "suspicious": False,
        }
        try:
            # The post-fix `defects4j test` already compiled the patched
            # checkout; recompiling here is cheap and idempotent, and
            # protects any future caller that invokes FixCheck without
            # having just run the test suite.
            compile_res = run_step(
                container, "defects4j compile", workdir,
                description="Compiling patched sources (for FixCheck)",
            )
            if not compile_res.ok:
                result["ok"] = False
                result["error"] = f"defects4j compile failed:\n{compile_res.output}"
                print("[fixcheck] WARNING: FixCheck skipped, compile failed.")
                return result

            dir_bin_tests = export_property(container, workdir, "dir.bin.tests")
            dir_src_tests = export_property(container, workdir, "dir.src.tests")
            cp_test = export_property(container, workdir, "cp.test")
            if not (dir_bin_tests and dir_src_tests and cp_test):
                result["ok"] = False
                result["error"] = (
                    "could not export dir.bin.tests / dir.src.tests / cp.test"
                )
                print(f"[fixcheck] WARNING: FixCheck skipped: {result['error']}")
                return result
            test_classes_path = os.path.join(workdir, dir_bin_tests)
            test_classes_src = os.path.join(workdir, dir_src_tests)

            for fqcn, methods in group_triggers_by_class(trigger_tests).items():
                try:
                    record = self._run_for_class(
                        container, workdir, fqcn, methods, trigger_method_sources,
                        test_classes_path, test_classes_src, cp_test,
                    )
                except Exception as exc:
                    print(f"[fixcheck] WARNING: FixCheck({fqcn}) crashed: {exc}")
                    record = {
                        "test_class": fqcn, "inputs_class": None, "run_dir": None,
                        "ok": False, "report": None, "scores": [], "max_score": 0.0,
                        "error": str(exc),
                    }
                result["inputs_class"][fqcn] = record["inputs_class"]
                result["per_test_class"].append(record)

            result["analyzed_test_classes"] = sum(
                1 for r in result["per_test_class"] if r["ok"]
            )
            result["failing_prefixes"] = sum(
                (r["report"]["crashing"] + r["report"]["assertion_failing"]) if r["report"] else 0
                for r in result["per_test_class"]
            )
            result["max_failure_similarity"] = max(
                (r["max_score"] for r in result["per_test_class"]), default=0.0
            )
            result["suspicious"] = (
                result["failing_prefixes"] > 0
                and result["max_failure_similarity"] >= self.similarity_threshold
            )
        except Exception as exc:
            print(f"[fixcheck] WARNING: FixCheck run failed: {exc}")
            result["ok"] = False
            result["error"] = str(exc)
        return result

    def _run_for_class(self, container, workdir, fqcn, methods, trigger_method_sources,
                        test_classes_path, test_classes_src, cp_test):
        """Run FixCheck for a single trigger test class.

        Returns a per-class record: ``{"test_class", "inputs_class", "run_dir",
        "ok", "report", "scores", "max_score", "error"?}``. Never raises --
        :meth:`run` treats every failure here as advisory.
        """
        simple_name = fqcn.rsplit(".", 1)[-1]
        run_dir = os.path.join(workdir, ".fixcheck", f"run_{simple_name}")
        os.makedirs(run_dir, exist_ok=True)

        method_sources = (trigger_method_sources or {}).get(fqcn, {})
        if self.inputs_class:
            # An explicit type is the caller's call; keep every trigger method.
            inputs_class, usable_methods = self.inputs_class, list(methods)
        else:
            inputs_class, usable_methods = select_fixcheck_inputs(methods, method_sources)

        record = {
            "test_class": fqcn,
            "inputs_class": inputs_class,
            "test_methods": usable_methods,
            "run_dir": run_dir,
            "ok": False,
            "report": None,
            "scores": [],
            "max_score": 0.0,
        }

        if inputs_class is None or not usable_methods:
            # Every literal sits inside an assertion (or the method is
            # inherited and so absent from this class's source, which
            # FixCheck parses on its own). Running it would end in
            # `IllegalArgumentException: No locals of type <T>` and no
            # report -- skipping is the same outcome, minutes faster and
            # self-explanatory.
            missing = [m for m in methods if m not in method_sources]
            detail = (
                f"trigger method(s) {missing} not declared in this class "
                "(inherited?); FixCheck only parses the named class's source"
                if missing else
                "no mutable literal outside assertions in the trigger method(s)"
            )
            record["skipped"] = True
            record["error"] = (
                f"{detail}; FixCheck cannot generate variations "
                "(override with an explicit inputs_class)"
            )
            print(f"[fixcheck] FixCheck({fqcn}): skipped -- {record['error']}")
            return record

        dropped = [m for m in methods if m not in usable_methods]
        if dropped:
            print(f"[fixcheck] FixCheck({fqcn}): analyzing {usable_methods}; "
                  f"skipping {dropped} (no {inputs_class} literal to mutate)")

        failure_log_path = fixcheck_failure_log_path(workdir, fqcn)
        if not os.path.exists(failure_log_path):
            record["error"] = f"missing pre-fix failure trace: {failure_log_path}"
            print(f"[fixcheck] WARNING: FixCheck({fqcn}): {record['error']}")
            return record

        props_text = build_fixcheck_properties(
            test_classes_path=test_classes_path,
            test_class=fqcn,
            test_methods=usable_methods,
            test_classes_src=test_classes_src,
            failure_log_path=failure_log_path,
            inputs_class=inputs_class,
            num_prefixes=self.num_prefixes,
            assertion_generator=self.assertion_generator,
        )
        props_path = os.path.join(run_dir, "fixcheck.properties")
        with open(props_path, "w", encoding="utf-8") as f:
            f.write(props_text)

        full_cp = f"{self.jar_path}:{_resolve_classpath(workdir, cp_test)}"
        cmd = f"java -cp {full_cp} org.imdea.fixcheck.FixCheck -p {props_path}"
        print(f"[fixcheck] Running FixCheck for {fqcn} ({len(usable_methods)} trigger "
              f"method(s), inputs-class={inputs_class}) ...")
        exec_result = exec_in_container(container, cmd, workdir=run_dir)
        _write_text(os.path.join(run_dir, "fixcheck.log"), exec_result.output)
        if not exec_result.ok:
            # FixCheck.main() normally exits 0 even when generation partially
            # fails; a non-zero exit means something more fundamental broke
            # (e.g. a bad classpath). Still try to read whatever it produced.
            print(f"[fixcheck] WARNING: FixCheck({fqcn}) exited "
                  f"{exec_result.exit_code}; treating as advisory failure.")

        output_dir = os.path.join(run_dir, "fixcheck-output")
        report_text = _read_text_or_none(os.path.join(output_dir, "report.csv"))
        scores_text = _read_text_or_none(os.path.join(output_dir, "scores-failing-tests.csv"))

        record["report"] = parse_fixcheck_report(report_text) if report_text is not None else None
        record["scores"] = parse_fixcheck_scores(scores_text) if scores_text is not None else []
        record["max_score"] = max(record["scores"], default=0.0)
        record["ok"] = record["report"] is not None
        if not record["ok"]:
            record["error"] = "report.csv missing or unparsable"
            print(f"[fixcheck] WARNING: FixCheck({fqcn}): {record['error']}")
        return record
