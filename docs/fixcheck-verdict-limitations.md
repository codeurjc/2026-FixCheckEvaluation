# Investigation: how much a FixCheck `suspicious: false` verdict is worth

**Date:** 2026-08-08
**Subjects:** Defects4J Lang 12 and Math 69, each patched with its **developer's
own fix** (`<id>b` diffed against the `<id>f` checkout), via
`test/e2e/test_fixcheck_devfix.py`.
**Summary:** two upstream defects, one line each, cancel out most of FixCheck's
detection power as vendored. With the default `previous-assertion` generator
the variations carry **no assertions at all**, so only crashes are observable;
and when `codellama` does produce a real assertion and catches a failure, the
similarity metric discards it as dissimilar because its stack-trace
normalization is dead on Java 9+. Every number below was measured, not
inferred.

## Why the developer's fix is the right probe

FixCheck's premise is that a patch which merely satisfies the trigger test —
rather than fixing the defect — will let some *variation* of that test fail the
same way the original bug did. Running it against the fix that **defines** the
bug as fixed should therefore produce `suspicious: false`. It did, on every
combination tried. The point of this investigation is that the verdict was
reached for the wrong reasons, so it would also have been `false` for a patch
that genuinely was overfitting.

## Defect 1 — `previous-assertion` strips the assertions and never restores them

`InputTransformer.transform` preserves the original assertions only for that
one generator:

```java
// fixcheck/src/main/java/org/imdea/fixcheck/transform/input/InputTransformer.java:50
if (!("previous-assertion".equals(FixCheckProperties.ASSERTION_GENERATOR)))
    removeAssertionsFromMethod(newMethod);
```

But `FixCheckProperties` never stores the option key — it stores the *resolved
class name*, because `loadProperties()` runs the value through
`AssertionGeneratorProperty.parseOption()`:

```java
// properties/FixCheckProperties.java:108
FixCheckProperties.ASSERTION_GENERATOR =
    AssertionGeneratorProperty.parseOption(prop.getProperty("assertion-generator"));
```

The run's own log confirms what ends up in there:

```
assertions generation: org.imdea.fixcheck.assertion.UsePreviousAssertGenerator
```

So the guard compares `"previous-assertion"` against a fully-qualified class
name, is **always** true, and the assertions are always stripped.
`UsePreviousAssertGenerator` then locates them correctly…

```
---> Original assertions to use: 1
[assertTrue(corrInstance.getCorrelationPValues().getEntry(0, 1) > 0);]
```

…and throws them away, because its re-append is commented out on the assumption
that nothing removed them:

```java
// assertion/UsePreviousAssertGenerator.java:28-30
// NOTE: Assertions are no longer appended in this step, since removal is not
// done when using previous assertions
//MethodDeclaration method = prefix.getMethod();
//originalAssertions.forEach(assertion -> method.getBody().get().addStatement(assertion));
```

Each half assumes the other did its job. Verified by inspection: **all 10
generated prefixes across both bugs contained zero assertions.**

### Consequences

- `passing` counts are vacuous — a test with no assertion passes unless it throws.
- `assertion_failing` is structurally always `0`.
- A crash is the only failure FixCheck can still observe. For a bug that
  manifests as a **wrong value** rather than a crash — Math 69's
  `assertTrue(pValue > 0)` — this mode cannot detect overfitting even in
  principle, because the very assertion that reveals the bug is the one dropped.

`assert-true` is no better, but deliberately so: it appends a literal
`assertTrue(true);` and its own Javadoc calls it *"just for testing"*. That the
two produce identical reports is the cheapest way to see the problem.

### Measured comparison

`NUM_PREFIXES = 5`, `--fixcheck-similarity-threshold 0.8`:

| bug | generator | total | passing | crashing | assertion-failing | max similarity | suspicious |
|---|---|---|---|---|---|---|---|
| Lang 12 | `previous-assertion` | 5 | 5 | 0 | 0 | 0.000 | No |
| Lang 12 | `assert-true` | 5 | 5 | 0 | 0 | 0.000 | No |
| Lang 12 | `codellama` | 5 | 5 | 0 | 0 | 0.000 | No |
| Math 69 | `previous-assertion` | 5 | 4 | 1 | 0 | 0.405 | No |
| Math 69 | `assert-true` | 5 | 4 | 1 | 0 | 0.406 | No |
| Math 69 | `codellama` | 5 | 2 | 2 | **1** | 0.474 | No |

Only `codellama` ever asserted anything real. On Math 69 it regenerated the
bug-revealing assertion verbatim…

```
assertions: [    assertTrue(corrInstance.getCorrelationPValues().getEntry(0, 1) > 0);]
```

…and the variation carrying it **failed**. The mutation was the loop's start
index, leaving 70 rows at zero:

```java
int dimension = 120;
double[][] data = new double[dimension][2];
for (int i = 70; i < dimension; i++) {          // original: int i = 0
    data[i][0] = i;
    data[i][1] = i + 1 / ((double) i + 1);
}
PearsonsCorrelation corrInstance = new PearsonsCorrelation(data);
assertTrue(corrInstance.getCorrelationPValues().getEntry(0, 1) > 0);
```

Under the other two generators that same variation passed silently.

## Defect 2 — the similarity metric is dominated by harness noise

That detection still did not flag the patch: `0.474 < 0.8`.
`FailureChecker.normalizeFailureTrace` is meant to drop the irrelevant tail of
a stack trace by cutting at the reflection frame:

```java
// checker/FailureChecker.java:34
if (line.contains("at sun.reflect.NativeMethodAccessorImpl.invoke0")) break;
```

Java 9 renamed `sun.reflect` to `jdk.internal.reflect`. On the image's JDK 11
the frame reads `java.base/jdk.internal.reflect.NativeMethodAccessorImpl.invoke0`,
the marker is absent from **both** traces, and nothing is ever trimmed.

Levenshtein then compares two traces produced by different runners:

- the **original** failure, captured by `defects4j test`, carries ~30 frames of
  Ant's `JUnitTask` / `Project.executeTarget` / `Launcher.main`;
- the **prefix** failure, produced in-process by FixCheck's `PrefixRunner` via
  `JUnitCore`, ends in `PrefixRunner.runPrefix` / `FixCheck.main`.

The meaningful heads are identical — same exception, same assertion, same test
method — while the tails are structurally incomparable. A secondary issue
compounds it: `normalizeFailureTrace` is applied only to the new trace, never
to `originalFailure`, which is used exactly as read from
`test-failure-trace-log`.

For Math 69's assertion-failing prefix:

| | similarity |
|---|---|
| as FixCheck computes it today | **0.490** |
| truncating both traces at the real JDK 11 frame | **0.828** |

Against the default `0.8` threshold, that is precisely the difference between a
flagged patch and a missed one.

## Secondary observations

- **Mutation has no notion of a value's role.** `InputTransformer` picks any
  literal of `inputs-class`, including array indices and loop bounds. Three of
  Math 69's five variations turned `data[i][0]` into `data[i][31]`, `[49]`,
  `[86]` on a `new double[dimension][2]`, dying of
  `ArrayIndexOutOfBoundsException` **inside the test**, before reaching the code
  under test. They tell us nothing about the patch, yet they inflate
  `failing_prefixes`; only the similarity gate keeps them from mattering.
- **Runs are not reproducible.** The literal to mutate and its replacement are
  chosen at random: the same bug and generator gave `4 passing / 1 crashing` on
  one run and `2 passing / 3 crashing` on the next. Aggregate over repetitions
  rather than trusting a single report.
- **Cost.** `codellama` spends one model call per prefix. On a local
  `codellama:7b`, assertion generation dominated everything else —
  `assertions_gen_time` of 125 s for Math 69 and 696 s for Lang 12's larger
  test, against ~2 s of actual prefix execution.

## How to read a verdict, for now

`analyzed_test_classes > 0` is **necessary** for `suspicious: false` to mean
anything — with nothing analyzed there is no evidence either way — but it is far
from sufficient. On these subjects the negative verdicts were false negatives
for two different reasons:

- with `previous-assertion` and `assert-true`, because the variations asserted
  nothing;
- with `codellama`, because a genuine detection was discarded by a broken
  similarity metric.

A fair caveat on this particular subject: even with a working metric, flagging
Math 69 would be debatable. The MATH-371 fix has a documented numerical limit
(p-values vanish past dimension 127 through `double` underflow), so the
variation explores a regime where the property fails by arithmetic rather than
by an incomplete patch. FixCheck cannot distinguish the two.

## Status

Both defects are one-line upstream bugs and **neither is patched here**:

| | file | fix |
|---|---|---|
| 1 | `transform/input/InputTransformer.java:50` | compare against the resolved class name (or uncomment the re-append in `UsePreviousAssertGenerator`) |
| 2 | `checker/FailureChecker.java:34` | also match `jdk.internal.reflect.…`, and normalize `originalFailure` too |

`fixcheck/` is a gitignored clone, so a local edit is lost on re-clone; making
either fix stick means forking, or applying a patch from
`scripts/buildFixcheck.sh` after the clone step. `--fixcheck-assertions` makes
it possible to measure the before/after.

## Reproducing

```bash
bash scripts/buildFixcheck.sh          # once
ollama cp codellama:7b codellama:latest # once; FixCheck hardcodes the bare tag

.venv/bin/python -m pytest test/e2e/test_fixcheck_devfix.py -v -s \
    --fixcheck-assertions previous-assertion,assert-true,codellama
```

Artifacts land in `logs/test/<Project>_<BugId>/<generator>/` — `fixcheck.log`
holds the prompts and the assertions the generator returned, and
`fixcheck-output/{passing,failing,non-compiling}-tests/` the generated prefix
sources and their failure traces.
