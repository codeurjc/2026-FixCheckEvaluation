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

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import time
from collections import namedtuple

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
# The parameters of FixCheck's own evaluation, agreed with its author for this
# project: 100 prefixes per bug-revealing test method, and a patch flagged when
# a failing prefix scores at least 0.4 against the original failure (upstream's
# experiments/results/rq1-effectiveness.py). The archived campaigns used 10 and
# 0.8.
DEFAULT_FIXCHECK_PREFIXES = 100
DEFAULT_FIXCHECK_ASSERTIONS = "previous-assertion"
DEFAULT_FIXCHECK_SIMILARITY_THRESHOLD = 0.4
# Wall-clock budget for one FixCheck run, i.e. one (trigger method, literal
# type) pair. A safety net rather than a working limit: each prefix has its own
# budget inside FixCheck and each model call its own timeout (below). Without
# any, Math 10 and 13 hung in FixCheck for hours in both campaigns on a mutated
# size that never ended.
DEFAULT_FIXCHECK_TIMEOUT = 14400
# Budget for running one prefix (FixCheck's prefix-timeout-seconds, patch 0008).
# Measured p99: 0.7 s; the maximum, 439 s, was a mutated derivation order.
DEFAULT_FIXCHECK_PREFIX_TIMEOUT = 60
# Timeout of each call to the assertion-generating model (measured p99: 46 s).
DEFAULT_FIXCHECK_LLM_TIMEOUT = 300
# Recorded in every result, so readers can tell this layout -- one run per
# (trigger method, literal type) -- from the one-run-per-class records of the
# archived campaigns.
FIXCHECK_RESULT_SCHEMA = "fixcheck-v2"
# coreutils `timeout` exit statuses: 124 = sent TERM at the deadline, 137 =
# needed the KILL from --kill-after.
_TIMEOUT_EXIT_CODES = (124, 137)

# Assertion generators that ask an Ollama daemon to write the assertions
# instead of reusing the original test's. ``CodeLlamaOllama`` and
# ``Llama3_1Ollama`` hardcode the endpoint *and* the model tag as
# ``private final`` fields, so neither is configurable through the
# ``.properties`` file -- the values here have to match the Java source exactly
# (fixcheck/src/main/java/org/imdea/fixcheck/assertion/).
FIXCHECK_OLLAMA_GENERATORS = {
    "codellama": "codellama",
    "llama3.1": "llama3.1",
}
# Also hardcoded in those two classes. Because it is *localhost*, the
# experiment container must share the host's network namespace to reach a
# daemon running on the host -- see :func:`needs_host_network`.
FIXCHECK_OLLAMA_URL = "http://localhost:11434"

# The generic generator (``assertion/OllamaGenerator.java``) takes the model
# and the endpoint from the configuration instead, selected as
# ``ollama:<model>[@[<host>:]<port>]``. The endpoint separator is ``@``
# because a colon is already part of Ollama's ``<model>:<version>`` tags.
FIXCHECK_OLLAMA_OPTION_PREFIX = "ollama"
DEFAULT_FIXCHECK_OLLAMA_HOST = "localhost"
DEFAULT_FIXCHECK_OLLAMA_PORT = 11434
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

OllamaBackend = namedtuple("OllamaBackend", "model host port base_url wanted_tag")


def _make_backend(model, host, port):
    """Build an :class:`OllamaBackend`, resolving the tag Ollama will match.

    A bare model name is resolved by Ollama to ``<name>:latest``, so that is
    the tag ``/api/tags`` has to list for the call to succeed.
    """
    return OllamaBackend(
        model=model, host=host, port=port,
        base_url=f"http://{host}:{port}",
        wanted_tag=model if ":" in model else f"{model}:latest",
    )


def parse_ollama_generator(assertion_generator):
    """Parse an ``ollama:<model>[@[<host>:]<port>]`` spec into a backend.

    Returns ``None`` when the string does not select the generic Ollama
    generator, so callers can use it as a test. Mirrors
    ``fixcheck/src/main/java/org/imdea/fixcheck/properties/OllamaProperty.java``
    -- keep the two in step.

    >>> parse_ollama_generator("ollama:gpt-oss:120b@1995").model
    'gpt-oss:120b'

    :raises ValueError: if the spec selects the generator but is malformed.
    """
    prefix = FIXCHECK_OLLAMA_OPTION_PREFIX
    if assertion_generator == prefix:
        raise ValueError(
            f"{assertion_generator!r} does not name a model; use "
            f"'{prefix}:<model>[@[<host>:]<port>]', e.g. '{prefix}:gpt-oss:120b@1995'"
        )
    if not assertion_generator.startswith(f"{prefix}:"):
        return None

    spec = assertion_generator[len(prefix) + 1:]
    # The model tag itself contains colons, so the endpoint is split off at
    # the last '@' -- a character Ollama model names cannot contain.
    model, sep, endpoint = spec.rpartition("@")
    if not sep:
        model, endpoint = spec, ""
    if not model:
        raise ValueError(
            f"{assertion_generator!r} does not name a model; use "
            f"'{prefix}:<model>[@[<host>:]<port>]'"
        )

    host, port = DEFAULT_FIXCHECK_OLLAMA_HOST, DEFAULT_FIXCHECK_OLLAMA_PORT
    if sep and not endpoint:
        raise ValueError(f"{assertion_generator!r} has nothing after '@'")
    if endpoint:
        host_part, colon, port_part = endpoint.rpartition(":")
        if not colon:
            # A bare number is a port; anything else is a host.
            if endpoint.isdigit():
                port = int(endpoint)
            else:
                host = endpoint
        else:
            host = host_part or host
            if not port_part.isdigit():
                raise ValueError(
                    f"{assertion_generator!r}: {port_part!r} is not a port number"
                )
            port = int(port_part)
        if not 0 < port < 65536:
            raise ValueError(f"{assertion_generator!r}: port {port} is out of range")
    return _make_backend(model, host, port)


def resolve_ollama_backend(assertion_generator):
    """The Ollama endpoint a generator will call, or ``None`` if it calls none.

    Covers both the two hardcoded legacy generators and the configurable
    ``ollama:<model>[@[<host>:]<port>]`` form.
    """
    if assertion_generator in FIXCHECK_OLLAMA_GENERATORS:
        return _make_backend(
            FIXCHECK_OLLAMA_GENERATORS[assertion_generator],
            DEFAULT_FIXCHECK_OLLAMA_HOST, DEFAULT_FIXCHECK_OLLAMA_PORT,
        )
    return parse_ollama_generator(assertion_generator)


def validate_assertion_generator(value):
    """argparse ``type`` for ``--fixcheck-assertions``.

    A plain ``choices=`` list cannot express the open-ended
    ``ollama:<model>`` form, so the check lives here.
    """
    if value in FIXCHECK_ASSERTION_GENERATORS:
        return value
    try:
        if parse_ollama_generator(value) is not None:
            return value
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc))
    raise argparse.ArgumentTypeError(
        f"invalid assertion generator {value!r}; expected one of "
        f"{FIXCHECK_ASSERTION_GENERATORS} or "
        f"'{FIXCHECK_OLLAMA_OPTION_PREFIX}:<model>[@[<host>:]<port>]'"
    )


def needs_host_network(assertion_generator):
    """True when FixCheck will call a service on the *host's* ``localhost``.

    The Ollama-backed generators post to a loopback address. Under Docker's
    default bridge network that resolves to the container's own loopback,
    where nothing is listening, so every assertion-generation call dies with a
    ``ConnectException``. Running the container with ``network_mode="host"``
    makes the host's daemon reachable under the very name the jar uses. A
    generator pointed at a non-loopback host needs no such thing.

    ``Experiment.py`` consults this when starting the container, since the
    network mode is fixed at creation time -- long before FixCheck runs.
    """
    try:
        backend = resolve_ollama_backend(assertion_generator)
    except ValueError:
        return False
    return backend is not None and backend.host in _LOOPBACK_HOSTS


def check_ollama_backend(container, assertion_generator):
    """Check the container can reach the Ollama model the generator wants.

    Returns ``None`` when everything is in place, otherwise an error string
    explaining what to fix. Without this, a missing daemon or an unpulled
    model surfaces only as a ``RuntimeException`` per prefix and an
    unexplained "report.csv missing or unparsable" for every trigger class.

    Note that a *bare* model name (``codellama``) is resolved by Ollama to
    ``codellama:latest``; having ``codellama:7b`` pulled is not enough, hence
    the exact-tag comparison.
    """
    backend = resolve_ollama_backend(assertion_generator)

    probe = exec_in_container(
        container, f"curl -s --max-time 10 {backend.base_url}/api/tags"
    )
    if not probe.ok or not probe.output.strip():
        return (
            f"assertion-generator={assertion_generator!r} needs an Ollama "
            f"daemon at {backend.base_url} reachable from inside the "
            "container, but it did not respond. Start Ollama on the host and "
            "make sure the container runs with host networking."
        )
    try:
        available = [m.get("name", "") for m in json.loads(probe.output).get("models", [])]
    except ValueError:
        return (
            f"unexpected response from {backend.base_url}/api/tags: "
            f"{probe.output[:200]!r}"
        )
    if backend.wanted_tag not in available:
        hint = (
            f"FixCheck hardcodes that name, so alias an existing tag to it, "
            f"e.g.: ollama cp <your-tag> {backend.wanted_tag}"
            if assertion_generator in FIXCHECK_OLLAMA_GENERATORS
            else "Pull it, or name an available tag in --fixcheck-assertions."
        )
        return (
            f"assertion-generator={assertion_generator!r} requests the model "
            f"{backend.model!r}, which Ollama resolves to "
            f"{backend.wanted_tag!r}, but only {available} are available. "
            f"{hint}"
        )
    return None

# Method names FixCheck treats as assertions and therefore refuses to mutate
# (``transform/input/InputTransformer.java``'s ``isAssertion``). It compares
# the method name alone, so a qualified ``Assert.assertEquals(...)`` counts too.
FIXCHECK_ASSERTION_CALLS = (
    "assertNotNull", "assertTrue", "assertFalse", "assertEquals",
    "assertNotEquals", "fail", "check",
)
_ASSERTION_STMT_RE = re.compile(
    rf"^\s*(?:[\w$]+\s*\.\s*)*(?:{'|'.join(FIXCHECK_ASSERTION_CALLS)})\s*\("
)

# The literal types FixCheck's InputTransformer can mutate
# (``transform/input/InputHelper.java``), in the order that breaks ties.
FIXCHECK_LITERAL_TYPES = ("java.lang.String", "int", "long", "double", "boolean")


def fixcheck_command(classpath, props_path, timeout_seconds=DEFAULT_FIXCHECK_TIMEOUT):
    """The shell command that runs FixCheck, bounded by ``timeout_seconds``.

    Wrapped in coreutils ``timeout`` (present in the Defects4J image) rather
    than timed from Python, because ``docker exec`` offers no timeout and a
    hung JVM inside the container would otherwise outlive this call.
    ``--kill-after`` escalates to KILL for a JVM that ignores TERM. A falsy
    ``timeout_seconds`` means unbounded, the historical behaviour.
    """
    java = f"java -cp {classpath} org.imdea.fixcheck.FixCheck -p {props_path}"
    if not timeout_seconds:
        return java
    return f"timeout --kill-after=30 {int(timeout_seconds)} {java}"


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


def fixcheck_failure_log_path(workdir, fqcn, method):
    """Path of one trigger method's pre-fix failure trace.

    One file per method rather than per class: FixCheck compares every prefix
    against the first failure in its file, so with several trigger methods'
    traces concatenated, every method after the first was scored against a
    failure that was not its own. Shared by :func:`write_fixcheck_failure_logs`
    (writer, pre-fix) and :meth:`FixCheckWrapper.run` (reader, post-fix) so the
    two never drift apart.
    """
    return os.path.join(workdir, ".fixcheck", "traces", f"{fqcn}.{method}.failing_tests")


def strip_defects4j_headers(trace):
    """Drop the ``--- Class::method`` lines Defects4J writes before each failure.

    FixCheck only removes such a line for DefectRepairing subjects, so ours
    entered the Levenshtein distance against prefix traces that never carry
    one -- some 74 characters of pure difference. Recomputed over the archived
    campaign, it alone kept 40 runs under a 0.8 similarity they otherwise
    reached.
    """
    return "\n".join(line for line in trace.splitlines() if not line.startswith("--- "))


def write_fixcheck_failure_logs(workdir, trigger_tests, trigger_raw):
    """Write one clean pre-fix failure trace per trigger method.

    FixCheck needs the *original* (pre-patch) failure trace, but by the time
    it runs the patch has already been applied and Defects4J's
    ``failing_tests`` file has been overwritten. This captures it early
    (before fix generation, from ``Experiment.py``'s pipeline) as
    ``fixcheck_failure_log_path(workdir, fqcn, method)``: the raw
    ``failing_tests`` content of that trigger run, without Defects4J's header
    lines (:func:`strip_defects4j_headers`).

    Args:
        workdir: The bug's checkout directory (shared host/container path).
        trigger_tests: List of ``"FQCN::method"`` strings.
        trigger_raw: The dict returned by ``Experiment.run_trigger_tests_raw``.

    Returns:
        Dict mapping each ``"FQCN::method"`` trigger to the path written.
    """
    paths = {}
    for trigger in dict.fromkeys(trigger_tests):
        fqcn, _, method = trigger.partition("::")
        path = fixcheck_failure_log_path(workdir, fqcn, method)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(strip_defects4j_headers(trigger_raw.get(trigger, ("", ""))[1]))
        paths[trigger] = path
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


def _method_body(source):
    """The text between a method's opening brace and its last closing brace.

    The opening brace is the first one outside parentheses and literals, so
    an annotation argument such as ``@SuppressWarnings({"x"})`` is not taken
    for it. Source without a brace is returned whole.
    """
    depth = 0
    quote = None
    i = 0
    while i < len(source):
        ch = source[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in ('"', "'"):
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(depth - 1, 0)
        elif ch == "{" and depth == 0:
            last = source.rfind("}")
            return source[i + 1:last] if last > i else source[i + 1:]
        i += 1
    return source


def _leaf_statements(body):
    """Split comment-free Java code into its innermost statements.

    Splitting at ``;``, ``{`` and ``}`` outside parentheses and literals
    yields the innermost statements, each with the brace depth it sits at
    (0 for a statement directly in the method body), while a loop header such
    as ``for (int i = 0; i < n; i++)``, whose ``;`` sit inside parentheses,
    stays whole. See :func:`_mutable_statements` for why the depth matters.

    Returns a list of ``(statement, depth)``.
    """
    leaves = []
    current = []
    depth = 0
    brace_depth = 0
    quote = None
    i = 0
    while i < len(body):
        ch = body[i]
        if quote:
            current.append(ch)
            if ch == "\\" and i + 1 < len(body):
                current.append(body[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in ('"', "'"):
            quote = ch
            current.append(ch)
        elif ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth = max(depth - 1, 0)
            current.append(ch)
        elif ch in ";{}" and depth == 0:
            leaf = "".join(current).strip()
            if leaf:
                leaves.append((leaf, brace_depth))
            current = []
            if ch == "{":
                brace_depth += 1
            elif ch == "}":
                brace_depth = max(brace_depth - 1, 0)
        else:
            current.append(ch)
        i += 1
    tail = "".join(current).strip()
    if tail:
        leaves.append((tail, brace_depth))
    return leaves


def _mutable_statements(test_method_source, assertions_removed=True):
    """Return the statements of a test method whose literals FixCheck can mutate.

    ``InputTransformer.getRandomInputKnownType`` collects candidate literals
    from every statement of the method except blocks and assertion calls --
    but a statement's literals include those of the statements nested in it.
    What it finds therefore depends on the assertion generator:

    - with any generator but ``previous-assertion``, FixCheck first removes
      every assertion call, at any depth. ``try { x.run(); fail("..."); }``
      offers no string then, which is why counting that ``fail`` message
      planned a ``java.lang.String`` run for Math 67 that died with
      ``IllegalArgumentException: No locals of type java.lang.String``;
    - with ``previous-assertion`` the assertions stay, and only one written
      directly in the method body is out of reach: a nested one is still
      reached through the ``if``/``try``/``for`` containing it.

    Counting a literal FixCheck cannot reach plans a run that dies without a
    report; this returns exactly the statements whose literals it can reach.
    """
    body = _method_body(_strip_java_comments(test_method_source))
    kept = [
        statement for statement, depth in _leaf_statements(body)
        if not (_ASSERTION_STMT_RE.match(statement) and (assertions_removed or depth == 0))
    ]
    return "\n".join(kept)


# String and char literals, matched in one left-to-right pass so that a quote
# character inside one of them is never taken as the start of the other kind.
_QUOTED_LITERAL_RE = re.compile(r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'')
_DOUBLE_LITERAL_RE = re.compile(
    r"(?<![\w.])(?:\d[\d_]*\.[\d_]*(?:[eE][+-]?\d+)?[fFdD]?"
    r"|\.\d[\d_]*(?:[eE][+-]?\d+)?[fFdD]?"
    r"|\d[\d_]*[eE][+-]?\d+[fFdD]?"
    r"|\d[\d_]*[fFdD])(?![\w.])"
)
_INT_LITERAL_RE = re.compile(
    r"(?<![\w.])(?:0[xX][\da-fA-F_]+|0[bB][01_]+|\d[\d_]*)([lL])?(?![\w.])"
)
_BOOLEAN_LITERAL_RE = re.compile(r"\b(?:true|false)\b")


def count_java_literals(source):
    """Count literal occurrences per FixCheck literal type.

    Mirrors JavaParser's literal kinds as ``InputHelper`` maps them: string
    literals; integer literals (decimal, hex, binary) as ``int``, or ``long``
    with an ``L`` suffix; floating-point literals, ``f``/``d`` suffixed or
    with an exponent, as ``double``; ``true``/``false``. Digits inside a
    string or char literal are not numbers, and char literals count as none
    of the types.
    """
    string_count = 0
    parts = []
    last = 0
    for match in _QUOTED_LITERAL_RE.finditer(source):
        if match.group().startswith('"'):
            string_count += 1
        parts.append(source[last:match.start()])
        # Blank the literal out, same length, so later spans are not shifted.
        parts.append(" " * len(match.group()))
        last = match.end()
    parts.append(source[last:])
    code = "".join(parts)

    double_count = len(_DOUBLE_LITERAL_RE.findall(code))
    without_doubles = _DOUBLE_LITERAL_RE.sub(lambda m: " " * len(m.group()), code)
    int_suffixes = _INT_LITERAL_RE.findall(without_doubles)
    long_count = sum(1 for suffix in int_suffixes if suffix)

    return {
        "java.lang.String": string_count,
        "int": len(int_suffixes) - long_count,
        "long": long_count,
        "double": double_count,
        "boolean": len(_BOOLEAN_LITERAL_RE.findall(code)),
    }


def count_mutable_literals(test_method_source, assertions_removed=True):
    """The literals FixCheck can mutate in a trigger method, per literal type.

    ``assertions_removed`` is False only for the ``previous-assertion``
    generator; see :func:`_mutable_statements`.
    """
    return count_java_literals(_mutable_statements(test_method_source, assertions_removed))


def generator_removes_assertions(assertion_generator):
    """Whether FixCheck strips a prefix's assertions before mutating it.

    Every generator but ``previous-assertion`` does (``InputTransformer``,
    fixed by patch 0001 to actually keep them for that one).
    """
    return assertion_generator != "previous-assertion"


def allocate_prefixes(literal_counts, total=DEFAULT_FIXCHECK_PREFIXES):
    """Split a method's prefix budget among the literal types it can mutate.

    A FixCheck run mutates literals of a single ``inputs-class``; choosing one
    type per method (as the archived campaigns did, with a heuristic that
    picked ``java.lang.String`` for 80% of methods) left every other kind of
    input untouched. Instead, each type present gets a share of ``total``
    proportional to its number of mutable literals -- at least one prefix each
    while the budget allows -- by largest remainder, so the shares add up to
    exactly ``total``. Ties go to the earlier type in
    :data:`FIXCHECK_LITERAL_TYPES`.

    Returns a dict ``{literal_type: prefixes}`` in that order, holding only
    the types that get prefixes; empty when nothing can be mutated.
    """
    present = [t for t in FIXCHECK_LITERAL_TYPES if literal_counts.get(t, 0) > 0]
    if not present or total <= 0:
        return {}
    if total < len(present):
        ranked = sorted(present, key=lambda t: (-literal_counts[t], FIXCHECK_LITERAL_TYPES.index(t)))
        chosen = set(ranked[:total])
        return {t: 1 for t in present if t in chosen}

    remaining = total - len(present)
    literals = sum(literal_counts[t] for t in present)
    quotas = {t: remaining * literal_counts[t] / literals for t in present}
    shares = {t: 1 + int(quotas[t]) for t in present}
    leftover = total - sum(shares.values())
    by_remainder = sorted(
        present,
        key=lambda t: (-(quotas[t] - int(quotas[t])), FIXCHECK_LITERAL_TYPES.index(t)),
    )
    for t in by_remainder[:leftover]:
        shares[t] += 1
    return shares


def derive_seed(*parts):
    """A deterministic seed for one FixCheck run, from what identifies it.

    Derived from the subject and the (test class, method, literal type) of the
    run -- never from the model -- so the patches of both models and the
    developer's fix for one bug are all exercised with the same mutations.
    A non-negative Java ``long``.
    """
    digest = hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFF_FFFF_FFFF_FFFF


def literal_type_dir(literal_type):
    """The directory name of a literal type's runs (``java.lang.String`` -> ``String``)."""
    return re.sub(r"[^\w]", "_", literal_type.rsplit(".", 1)[-1])


def plan_fixcheck_runs(trigger_tests, trigger_method_sources,
                       total=DEFAULT_FIXCHECK_PREFIXES, inputs_class=None,
                       assertions_removed=True):
    """Decide which FixCheck runs a set of trigger tests needs.

    One run per trigger method and literal type, the method's ``total``
    prefixes split among the types it can mutate (:func:`allocate_prefixes`,
    counted as :func:`count_mutable_literals` with ``assertions_removed``).
    With ``inputs_class`` given, every method gets a single run of that type
    with the whole budget instead.

    A method FixCheck cannot work on is reported instead of planned: one whose
    source is not in its own class (inherited -- FixCheck parses the named
    class's file only), or one without a single mutable literal.

    Returns ``(runs, skipped)``: run specs ``{"test_class", "method",
    "inputs_class", "num_prefixes", "literal_counts"}`` and skip records
    ``{"test_class", "method", "reason"}``.
    """
    runs, skipped = [], []
    for fqcn, methods in group_triggers_by_class(trigger_tests).items():
        sources = (trigger_method_sources or {}).get(fqcn, {})
        for method in methods:
            source = sources.get(method)
            if source is None:
                skipped.append({
                    "test_class": fqcn, "method": method,
                    "reason": "not declared in this class (inherited?); FixCheck "
                              "only parses the named class's source",
                })
                continue
            counts = count_mutable_literals(source, assertions_removed)
            allocation = {inputs_class: total} if inputs_class else allocate_prefixes(counts, total)
            if not allocation:
                skipped.append({
                    "test_class": fqcn, "method": method,
                    "reason": "no mutable literal outside assertions",
                })
                continue
            for literal_type, prefixes in allocation.items():
                runs.append({
                    "test_class": fqcn, "method": method,
                    "inputs_class": literal_type, "num_prefixes": prefixes,
                    "literal_counts": counts,
                })
    return runs, skipped


def build_fixcheck_properties(test_classes_path, test_class, test_methods,
                               test_classes_src, failure_log_path, inputs_class,
                               num_prefixes, assertion_generator, *,
                               subject_classpath=None, output_dir=None, seed=None,
                               prefix_timeout_seconds=None, ollama_timeout_seconds=None,
                               ollama_temperature=None, ollama_seed=None):
    """Render a FixCheck ``.properties`` file for one FixCheck run.

    Key names mirror
    ``fixcheck/src/main/java/org/imdea/fixcheck/properties/FixCheckProperties.java``
    (``loadProperties()``) exactly. ``assertion_generator`` is one of the CLI
    option keys from ``AssertionGeneratorProperty.java`` (e.g.
    ``previous-assertion``), not the Java class name it resolves to.

    The keyword-only options are the properties added by
    ``scripts/fixcheck-patches/`` (0005, 0008-0010); each is written only
    when given, so an omitted one keeps FixCheck's previous behaviour.
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
    optional = [
        ("subject-classpath", subject_classpath),
        ("output-dir", output_dir),
        ("seed", seed),
        ("prefix-timeout-seconds", prefix_timeout_seconds),
        ("ollama-timeout-seconds", ollama_timeout_seconds),
        ("ollama-temperature", ollama_temperature),
        ("ollama-seed", ollama_seed),
    ]
    lines += [f"{key}={value}" for key, value in optional if value is not None]
    return "\n".join(lines) + "\n"


def parse_fixcheck_report(report_csv_text):
    """Parse FixCheck's one-row ``report.csv`` into a dict.

    The header is defined in ``writer/ReportWriter.java``. The report has no
    "non-compiling" column, so it is recovered as ``total - passing -
    crashing - assertion_failing - timed_out`` (FixCheck's prefix buckets --
    non-compiling, passing, crashing, assertion-failing, timed out -- are a
    partition of every generated prefix; see ``FixCheck.savePrefix()``), where
    ``total`` is the report's ``output_prefixes`` column. ``timed_out`` is
    ``None`` for a report from before patch 0008, which had no such column:
    not measured, rather than zero. Likewise ``assertion_generation_failed``
    (prefixes whose generator threw, e.g. an Ollama call outliving its
    timeout) before patch 0012; it is subtracted from ``non_compiling`` too.

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
    timed_out = as_int("timed_out_prefixes")
    assertion_generation_failed = as_int("assertion_generation_failed_prefixes")

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
        "timed_out": timed_out,
        "assertion_generation_failed": assertion_generation_failed,
        "non_compiling": (total - passing - crashing - assertion_failing
                          - (timed_out or 0) - (assertion_generation_failed or 0)),
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


_PREFIX_HEADER_RE = re.compile(r"^PREFIX \d+ of \d+$", re.M)
_OUTCOME_RE = re.compile(
    r"^---> prefix (passed|crashed|failed assertion|did not compile|timed out|assertion generation failed)$",
    re.M,
)
_SIMILARITY_RE = re.compile(r"^---> failure similarity: ([-\d.Ee]+)$", re.M)
_TRANSFORMATION_PREFIX = "---> transformation: "
# The outcomes that are scored against the original failure.
FIXCHECK_FAILING_OUTCOMES = ("crashed", "failed assertion")


def _parse_transformation(line):
    """Split ``[<old>:<inputs-class>] replaced by [<new>:<class>]`` into its parts.

    Split at the *last* separator, since the literals themselves may contain
    brackets and colons. Returns ``(old, new)``, or ``(None, None)``.
    """
    text = line[len(_TRANSFORMATION_PREFIX):].strip()
    head, sep, tail = text.rpartition("] replaced by [")
    if not sep or not head.startswith("[") or not tail.endswith("]"):
        return None, None
    old = head[1:].rpartition(":")[0]
    new = tail[:-1].rpartition(":")[0]
    return old, new


def parse_fixcheck_variations(log_text):
    """One record per generated prefix, in generation order, from ``fixcheck.log``.

    ``report.csv`` only counts prefixes; the log is where each one's
    mutation, outcome and similarity score are. Each record is
    ``{"index", "original", "replacement", "identity", "outcome", "score",
    "assertions_generated"}``. ``identity`` marks a mutation that replaced a
    literal by the same value: the trigger test passes on every patch FixCheck
    analyses, so such a prefix failing measures the harness, not the patch
    (37.7% of them failed in the archived campaign). ``score`` is ``None``
    for a prefix that was not scored.
    """
    if not log_text:
        return []
    headers = list(_PREFIX_HEADER_RE.finditer(log_text))
    variations = []
    for position, header in enumerate(headers):
        end = headers[position + 1].start() if position + 1 < len(headers) else len(log_text)
        block = log_text[header.end():end]
        original = replacement = None
        for line in block.splitlines():
            if line.startswith(_TRANSFORMATION_PREFIX):
                original, replacement = _parse_transformation(line)
                break
        outcomes = _OUTCOME_RE.findall(block)
        scores = _SIMILARITY_RE.findall(block)
        try:
            score = float(scores[-1]) if scores else None
        except ValueError:
            score = None
        variations.append({
            "index": position,
            "original": original,
            "replacement": replacement,
            "identity": original is not None and original == replacement,
            "outcome": outcomes[-1] if outcomes else None,
            "score": score,
            "assertions_generated": "---> assertion generator:" in block,
        })
    return variations


def _is_scored(variation):
    return variation.get("outcome") in FIXCHECK_FAILING_OUTCOMES and variation.get("score") is not None


def summarize_fixcheck_runs(runs, skipped, similarity_threshold):
    """Aggregate a subject's FixCheck runs into its verdict and counts.

    ``suspicious`` holds when any scored prefix reaches the threshold -- the
    ``prediction`` of upstream's ``rq1-effectiveness.py``. Only runs that
    produced a report count as analysed; ``analyzed_test_classes`` keeps its
    old meaning (distinct classes with at least one analysed run), so readers
    of the archived campaigns' records still work. ``max_failure_similarity``
    is ``None`` when no prefix was scored: nothing was measured.
    """
    analyzed = [r for r in runs if r.get("ok")]
    reports = [r["report"] for r in analyzed if r.get("report")]
    variations = [v for r in analyzed for v in (r.get("variations") or [])]
    scores = [v["score"] for v in variations if _is_scored(v)]
    identity = [v for v in variations if v.get("identity")]
    timed_out = [rep.get("timed_out") for rep in reports]
    generation_failed = [rep.get("assertion_generation_failed") for rep in reports]
    return {
        "planned_runs": len(runs),
        "analyzed_runs": len(analyzed),
        "timed_out_runs": sum(1 for r in runs if r.get("timed_out")),
        "analyzed_methods": len({(r["test_class"], r["method"]) for r in analyzed}),
        "analyzed_test_classes": len({r["test_class"] for r in analyzed}),
        "skipped_methods": len(skipped),
        "generated_prefixes": sum(rep["total"] for rep in reports),
        "failing_prefixes": sum(rep["crashing"] + rep["assertion_failing"] for rep in reports),
        "non_compiling_prefixes": sum(rep["non_compiling"] for rep in reports),
        "timed_out_prefixes": (
            None if not reports or any(t is None for t in timed_out) else sum(timed_out)
        ),
        "assertion_generation_failed_prefixes": (
            None if not reports or any(g is None for g in generation_failed) else sum(generation_failed)
        ),
        "scored_prefixes": len(scores),
        "max_failure_similarity": max(scores) if scores else None,
        "suspicious": any(score >= similarity_threshold for score in scores),
        "identity_prefixes": len(identity),
        "identity_failing_prefixes": sum(
            1 for v in identity if v.get("outcome") in FIXCHECK_FAILING_OUTCOMES
        ),
    }


def fixcheck_run_subdir(run):
    """``<TestClass>/<method>/<literal type>``: where a run's artifacts go."""
    return os.path.join(
        run["test_class"].rsplit(".", 1)[-1], run["method"], literal_type_dir(run["inputs_class"])
    )


def copy_fixcheck_artifacts(result, dest_root):
    """Copy every run's directory out of the checkout, under ``dest_root``.

    Each run directory holds ``fixcheck.properties``, ``fixcheck.log`` and
    ``fixcheck-output/`` (``report.csv``, the scores and the generated prefix
    sources); it lands at ``dest_root/<TestClass>/<method>/<literal type>/``.
    Returns how many were copied.
    """
    copied = 0
    for run in (result or {}).get("runs") or []:
        run_dir = run.get("run_dir")
        if run_dir and os.path.isdir(run_dir):
            shutil.copytree(run_dir, os.path.join(dest_root, fixcheck_run_subdir(run)),
                            dirs_exist_ok=True)
            copied += 1
    return copied


def missing_test_classes(test_classes_path, trigger_tests):
    """The trigger test classes with no compiled ``.class`` under ``test_classes_path``."""
    return [
        fqcn for fqcn in group_triggers_by_class(trigger_tests)
        if not os.path.isfile(os.path.join(test_classes_path, *fqcn.split(".")) + ".class")
    ]


def _resolve_classpath(workdir, cp_string):
    """Make every ``:``-separated classpath entry absolute.

    Defects4J's ``cp.*`` exports may be workdir-relative. FixCheck reads the
    class path from its properties file and opens it from its own working
    directory, so each entry is anchored to ``workdir`` rather than left to
    depend on where FixCheck happens to run. Already-absolute entries are left
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
    """Runs FixCheck over the trigger tests of a patched checkout.

    Configuration is passed directly to ``__init__`` rather than through
    ``Experiment.py``'s argparse ``Namespace``, so a wrapper can be built and
    exercised on its own (see ``test/e2e/test_fixcheck_devfix.py``) without
    constructing a fake CLI-args object.
    """

    def __init__(self, num_prefixes=DEFAULT_FIXCHECK_PREFIXES,
                 assertion_generator=DEFAULT_FIXCHECK_ASSERTIONS,
                 similarity_threshold=DEFAULT_FIXCHECK_SIMILARITY_THRESHOLD,
                 inputs_class=None, jar_path=FIXCHECK_JAR,
                 timeout_seconds=DEFAULT_FIXCHECK_TIMEOUT,
                 prefix_timeout_seconds=DEFAULT_FIXCHECK_PREFIX_TIMEOUT,
                 llm_timeout_seconds=DEFAULT_FIXCHECK_LLM_TIMEOUT):
        self.num_prefixes = num_prefixes
        self.assertion_generator = assertion_generator
        self.similarity_threshold = similarity_threshold
        self.inputs_class = inputs_class
        self.jar_path = jar_path
        self.timeout_seconds = timeout_seconds
        self.prefix_timeout_seconds = prefix_timeout_seconds
        self.llm_timeout_seconds = llm_timeout_seconds

    def run(self, container, workdir, trigger_tests, trigger_method_sources,
            subject_id=None, runs=None, skipped=None):
        """Run FixCheck on an already-patched, already-plausible checkout.

        Only meaningful once the patch is applied and every trigger test
        passes (see ``Experiment.main()``'s ``triggers_fixed`` gate) --
        FixCheck starts from the bug-revealing test(s), generates small input
        variations, runs them against the patched program, and flags the
        ones that still fail the same way as the original bug as evidence
        the patch is overfitting rather than genuinely correct.

        One FixCheck run per trigger method and literal type
        (:func:`plan_fixcheck_runs`), each with its own pre-fix trace, seed
        (:func:`derive_seed` over ``subject_id`` and the run) and prefix
        budget. ``runs``/``skipped`` replace that plan with an explicit one --
        run specs may then also carry ``test_methods`` and
        ``failure_log_path`` -- to reproduce a configuration chosen by hand.

        FixCheck is advisory: any exception, or a missing/unparsable report,
        is logged as a ``[fixcheck] WARNING`` and folded into the returned
        dict's ``ok``/``error`` fields rather than raised, so it never aborts
        the experiment and never affects ``fixed``.

        Returns:
            A dict with ``schema`` (:data:`FIXCHECK_RESULT_SCHEMA`), ``ran``,
            ``ok``, the configuration, ``runs`` (one record per FixCheck run,
            see :meth:`_run_one`), ``skipped`` (methods FixCheck cannot work
            on, with the reason) and the aggregate of
            :func:`summarize_fixcheck_runs`, verdict included.

            ``analyzed_runs > 0`` is necessary for ``suspicious: False`` to
            mean anything -- with nothing analyzed there is no evidence either
            way -- but it is far from sufficient; see
            docs/fixcheck-verdict-limitations.md before reporting one as a
            result.
        """
        result = {
            "schema": FIXCHECK_RESULT_SCHEMA,
            "ran": True,
            "ok": True,
            "subject_id": subject_id,
            "assertion_generator": self.assertion_generator,
            "num_prefixes": self.num_prefixes,
            "similarity_threshold": self.similarity_threshold,
            "inputs_class": self.inputs_class,
            "timeout_seconds": self.timeout_seconds,
            "prefix_timeout_seconds": self.prefix_timeout_seconds,
            "llm_timeout_seconds": self.llm_timeout_seconds,
            "runs": [],
            "skipped": [],
            **summarize_fixcheck_runs([], [], self.similarity_threshold),
        }
        try:
            # An LLM-backed generator is worth checking before anything else:
            # if the daemon or the model is missing, every prefix's assertion
            # call throws and the only symptom is an empty report per class.
            if resolve_ollama_backend(self.assertion_generator) is not None:
                backend_error = check_ollama_backend(
                    container, self.assertion_generator
                )
                if backend_error:
                    result["ok"] = False
                    result["error"] = backend_error
                    print(f"[fixcheck] WARNING: FixCheck skipped: {backend_error}")
                    return result

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

            # `defects4j compile` does not leave every project's test classes
            # in place: right after it Mockito's target/test-classes is empty,
            # so every prefix failed to compile against test-support classes
            # such as org.mockitoutil.TestBase -- 20 of the 64 runs the
            # archived campaign lost to non-compiling prefixes. Running one
            # trigger test compiles them all.
            missing = missing_test_classes(test_classes_path, trigger_tests)
            if missing:
                trigger = next(t for t in trigger_tests if t.partition("::")[0] in missing)
                print(f"[fixcheck] Test classes not compiled ({', '.join(missing)}); "
                      f"compiling them by running {trigger}")
                exec_in_container(container, f"defects4j test -t {trigger}", workdir=workdir)
                result["compiled_test_classes"] = True
                still_missing = missing_test_classes(test_classes_path, trigger_tests)
                if still_missing:
                    print(f"[fixcheck] WARNING: still no compiled test class for "
                          f"{', '.join(still_missing)}; their prefixes will not compile.")

            if runs is None:
                runs, skipped = plan_fixcheck_runs(
                    trigger_tests, trigger_method_sources, self.num_prefixes, self.inputs_class,
                    assertions_removed=generator_removes_assertions(self.assertion_generator),
                )
            result["skipped"] = list(skipped or [])
            for skip in result["skipped"]:
                print(f"[fixcheck] FixCheck({skip['test_class']}::{skip['method']}): "
                      f"skipped -- {skip['reason']}")

            for index, spec in enumerate(runs, start=1):
                try:
                    record = self._run_one(
                        container, workdir, spec, subject_id,
                        test_classes_path, test_classes_src, cp_test, index, len(runs),
                    )
                except Exception as exc:
                    print(f"[fixcheck] WARNING: FixCheck({spec['test_class']}::"
                          f"{spec['method']}, {spec['inputs_class']}) crashed: {exc}")
                    record = {
                        **spec, "run_dir": None, "ok": False, "timed_out": False,
                        "report": None, "variations": [], "max_score": None,
                        "error": str(exc),
                    }
                result["runs"].append(record)

            result.update(
                summarize_fixcheck_runs(result["runs"], result["skipped"], self.similarity_threshold)
            )
        except Exception as exc:
            print(f"[fixcheck] WARNING: FixCheck run failed: {exc}")
            result["ok"] = False
            result["error"] = str(exc)
        return result

    def _run_one(self, container, workdir, spec, subject_id,
                 test_classes_path, test_classes_src, cp_test, index=1, total=1):
        """Run FixCheck once: one trigger method, one literal type.

        FixCheck runs from the checkout's root -- tests that open files
        relative to it (Compress's ``src/test/resources/...``) failed from a
        scratch directory -- with its output sent to the run's own directory.
        The subject goes in ``subject-classpath`` rather than on ``java -cp``,
        so FixCheck defines each prefix in the same class loader as the
        subject (patch 0005).

        Returns a run record: the spec plus ``seed``, ``run_dir``, ``ok``,
        ``timed_out`` (the run's own budget), ``exit_code``, ``seconds``,
        ``report``, ``variations`` (:func:`parse_fixcheck_variations`),
        ``max_score`` and ``error`` when something went wrong. Never raises
        for a FixCheck failure -- :meth:`run` treats it as advisory.
        """
        fqcn, method = spec["test_class"], spec["method"]
        literal_type, prefixes = spec["inputs_class"], spec["num_prefixes"]
        run_dir = os.path.join(workdir, ".fixcheck", "runs", fixcheck_run_subdir(spec))
        # A run directory left by an earlier attempt must not lend it its report.
        shutil.rmtree(run_dir, ignore_errors=True)
        os.makedirs(run_dir)
        seed = derive_seed(subject_id, fqcn, method, literal_type)
        record = {
            **spec, "seed": seed, "run_dir": run_dir, "ok": False, "timed_out": False,
            "exit_code": None, "seconds": None, "report": None, "variations": [],
            "max_score": None,
        }

        failure_log_path = spec.get("failure_log_path") or fixcheck_failure_log_path(
            workdir, fqcn, method
        )
        if not os.path.exists(failure_log_path):
            record["error"] = f"missing pre-fix failure trace: {failure_log_path}"
            print(f"[fixcheck] WARNING: FixCheck({fqcn}::{method}): {record['error']}")
            return record

        uses_llm = resolve_ollama_backend(self.assertion_generator) is not None
        props_text = build_fixcheck_properties(
            test_classes_path=test_classes_path,
            test_class=fqcn,
            test_methods=spec.get("test_methods") or [method],
            test_classes_src=test_classes_src,
            failure_log_path=failure_log_path,
            inputs_class=literal_type,
            num_prefixes=prefixes,
            assertion_generator=self.assertion_generator,
            subject_classpath=_resolve_classpath(workdir, cp_test),
            output_dir=os.path.join(run_dir, "fixcheck-output"),
            seed=seed,
            prefix_timeout_seconds=self.prefix_timeout_seconds or None,
            ollama_timeout_seconds=self.llm_timeout_seconds if uses_llm else None,
            # The same prefix gets the same assertions on every run.
            ollama_temperature=0 if uses_llm else None,
            ollama_seed=seed if uses_llm else None,
        )
        props_path = os.path.join(run_dir, "fixcheck.properties")
        with open(props_path, "w", encoding="utf-8") as f:
            f.write(props_text)

        cmd = fixcheck_command(self.jar_path, props_path, self.timeout_seconds)
        print(f"[fixcheck] Running FixCheck {index}/{total} for {fqcn}::{method} "
              f"({prefixes} prefixes, inputs-class={literal_type}, budget "
              f"{self.timeout_seconds or 'unbounded'}s) ...")
        started = time.time()
        exec_result = exec_in_container(container, cmd, workdir=workdir)
        record["seconds"] = round(time.time() - started, 1)
        record["exit_code"] = exec_result.exit_code
        _write_text(os.path.join(run_dir, "fixcheck.log"), exec_result.output)
        record["timed_out"] = bool(
            self.timeout_seconds and exec_result.exit_code in _TIMEOUT_EXIT_CODES
        )
        if record["timed_out"]:
            print(f"[fixcheck] WARNING: FixCheck({fqcn}::{method}, {literal_type}) "
                  f"exceeded its {self.timeout_seconds}s budget and was stopped; "
                  "advisory failure, the patch's verdict is unaffected.")
        elif not exec_result.ok:
            # FixCheck.main() normally exits 0 even when generation partially
            # fails; a non-zero exit means something more fundamental broke
            # (e.g. a bad classpath). Still try to read whatever it produced.
            print(f"[fixcheck] WARNING: FixCheck({fqcn}::{method}, {literal_type}) "
                  f"exited {exec_result.exit_code}; treating as advisory failure.")

        report_text = _read_text_or_none(os.path.join(run_dir, "fixcheck-output", "report.csv"))
        record["report"] = parse_fixcheck_report(report_text) if report_text is not None else None
        record["variations"] = parse_fixcheck_variations(exec_result.output)
        scored = [v["score"] for v in record["variations"] if _is_scored(v)]
        record["max_score"] = max(scored) if scored else None
        record["ok"] = record["report"] is not None and not record["timed_out"]
        if record["timed_out"]:
            record["error"] = f"FixCheck timed out after {self.timeout_seconds}s"
        elif not record["ok"]:
            record["error"] = "report.csv missing or unparsable"
            print(f"[fixcheck] WARNING: FixCheck({fqcn}::{method}, {literal_type}): "
                  f"{record['error']}")
        return record
