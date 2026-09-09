# Why so many FixCheck verdicts are vacuous

A FixCheck verdict is **vacuous** when `fixcheck_suspicious` is `false` but
`analyzed_test_classes == 0`: FixCheck ran and never got as far as analysing a
single test class. That is not "the patch is fine"; it is "I could not look".
The two are identical in the JSON if you only read the boolean, which is why
`summarize_campaign.py` keeps the two fields apart.

Companion documents: [fixcheck-verdict-limitations.md](fixcheck-verdict-limitations.md)
(the verdict's limitations in general) and [audit-2026-09.md](audit-2026-09.md)
(the data audit this came out of). A Spanish version of this document is kept at
`notes/fixcheck-veredictos-vacuos.md`, and the two upstream FixCheck defects we
fixed are written up in Spanish at `notes/fixcheck-defects.md`.

## How many

Over the **paired set** (the 847 bugs both models completed), which is the
population the notebook uses:

| | plausible | FixCheck analysed ≥1 class | vacuous | % vacuous |
|---|---:|---:|---:|---:|
| qwen3.6:35b | 455 | 337 | 118 | 25.9% |
| gpt-oss:120b | 483 | 366 | 117 | 24.2% |
| **total** | **938** | **703** | **235** | **25.1%** |

"Plausible" = the patch applied, **compiled**, and the trigger tests pass.

**Mind the denominator.** Counting every completed run instead of the paired
set, the plausible patches are **941**, not 938: three plausible patches (2 qwen,
1 gpt-oss) belong to bugs only one model finished. The vacuous count is **235
either way** — only the denominator moves, 25.1% against 25.0%. This document
uses the paired set so its figures match `analysis/analysis.ipynb`.

One more warning, because it is the trap that prompted the audit: FixCheck was
also **invoked** on the 203 patches that never compiled — `Experiment.py` gated
it on `triggers_fixed`, computed before the compile check — and aborted on all
203 with `ok: false`. Those are not vacuous verdicts; they are not verdicts at
all, and they appear in none of the columns above.

> **Careful with earlier figures.** A 38% (438 of 1144) was reported at one
> point. That count predates the fix for patches that applied without compiling:
> 203 runs entered the "plausible" set without compiling, FixCheck was launched
> on them and could analyse nothing. Once they stop counting as plausible they
> leave the denominator *and* the numerator, and the rate drops to 25%. See the
> "Evaluation fix" section of [campaign.md](campaign.md).

## Where the 235 come from

There is no single cause: there are five, split almost evenly between "FixCheck
decides not to try" (104, 44%) and "FixCheck tries and breaks" (131, 56%).

| # | kind | cause | example |
|---:|---|---|---|
| 84 | *skip* | no mutable literal outside the assertions | `qwen3.6:35b/JxPath/Bug_13` |
| 54 | crash | `IllegalArgumentException: No locals of type <T>` | `qwen3.6:35b/Math/Bug_67` |
| 47 | crash | `NullPointerException` (a prefix did not compile) | `qwen3.6:35b/Mockito/Bug_31` |
| 30 | crash | another exception (**26 are `IllegalAccessError`**) | `qwen3.6:35b/Closure/Bug_111` |
| 20 | *skip* | the trigger test is inherited | `qwen3.6:35b/JxPath/Bug_16` |

### 1. Well-factored tests are opaque (84 cases)

FixCheck parses **only the body of the trigger method you name it**. JxPath 13
is the pure case:

```java
public void testCreateAndSetAttributeDOM() {
    doTestCreateAndSetAttribute(DocumentContainer.MODEL_DOM);
}
```

One line delegating to a helper, and its only argument is a **constant
reference, not a literal**. There is nothing to mutate. And this is not exotic:
it is exactly the style recommended for tests parameterised by variant. The
better factored the test, the less FixCheck sees.

### 2. The `inputs-class` heuristic and FixCheck do not look at the same thing (54 cases)

This one is **ours**, in `FixCheckWrapper._mutable_statements`, not FixCheck's.
On Math 67 the heuristic chose `inputs_class = java.lang.String` because it
counted 4 `String` literals in the body of `testQuinticMin`. But all four are
error messages inside `try` blocks:

```java
try {
    minimizer.getOptima();
    fail("an exception should have been thrown");
} catch (IllegalStateException ise) {
    // expected
}
```

FixCheck ignores **every statement that is a block** (`try`, `for`, `if`) as
well as the assertions. Our `_split_java_statements` returns the `try {...}` as
a single statement that does not begin with `assert`/`fail`, so
`_ASSERTION_STMT_RE` does not filter it, it is kept, and the literals inside get
counted. Result: we propose `String`, FixCheck looks and finds none:

```
IllegalArgumentException: No locals of type java.lang.String
    at InputTransformer.getRandomInputKnownType:147
```

**These 54 are recoverable**: it would be enough to discard block statements in
`_mutable_statements` too. That would take the analysis from 703 to ~757
effective runs. It is the only one of the five mechanisms on our side of the
fence.

### 3. FixCheck's classloader cannot reach the project's superclasses (26 cases)

Closure 111 and 25 more:

```
java.lang.IllegalAccessError: class ...SimilarPrefixInputTransformer0 cannot access
its abstract superclass com.google.javascript.jscomp.CompilerTypeTestCase
(... unnamed module of loader org.imdea.fixcheck.compilation.InMemoryClassLoader;
 ... unnamed module of loader 'app')
```

FixCheck compiles the mutated prefix in memory with its own
`InMemoryClassLoader`, but the test's base class was loaded by the application
classloader. Java treats the two loaders as distinct *runtime packages*, so
access to a package-private abstract superclass fails. It hits projects with
their own test hierarchies — Closure above all.

### 4. The generated prefixes do not compile (47 cases)

The `NullPointerException` is the symptom, not the cause: FixCheck generates the
variation, `javac` rejects it, and the code carries on with a null object
instead of reporting the compile failure. This is the role-blind mutation
described in [fixcheck-verdict-limitations.md](fixcheck-verdict-limitations.md):
a literal is changed without checking the result still type-checks.

### 5. Inherited tests (20 cases)

JxPath 16's `testAxisFollowing` / `testAxisPreceding` are declared in the parent
class. FixCheck opens the source file of the class you named and does not find
the method. Same mechanism as point 1: it reads one file.

## The vacuous verdicts are not spread at random

Vacuous-verdict rate per project, over that project's plausible patches (both
models together):

| project | plausible | vacuous | % |
|---|---:|---:|---:|
| Mockito | 42 | 32 | 76% |
| Collections | 34 | 18 | 53% |
| JxPath | 17 | 9 | 53% |
| Lang | 83 | 35 | 42% |
| Chart | 30 | 11 | 37% |
| Math | 133 | 45 | 34% |
| Codec | 22 | 7 | 32% |
| Closure | 127 | 33 | 26% |
| JacksonDatabind | 115 | 29 | 25% |
| JacksonCore | 26 | 6 | 23% |
| Gson | 24 | 3 | 12% |
| Cli | 56 | 4 | 7% |
| Jsoup | 111 | 2 | 2% |
| Compress | 64 | 1 | 2% |
| Csv | 23 | 0 | 0% |
| JacksonXml | 5 | 0 | 0% |
| Time | 26 | 0 | 0% |
| **TOTAL** | **938** | **235** | **25%** |

The range runs from **76% (Mockito)** to **0% (Time, Csv, JacksonXml)**. It is a
property of **each project's test style**, not of the model or the patch:
Mockito uses test hierarchies and delegating helpers everywhere, while Time and
Csv have flat tests full of literals.

## What this means for the result

A "0 suspicious out of 703" does **not** say the patches are not overfitted. It
says two things in sequence:

1. Of the 938 plausible patches, FixCheck managed to build and run variations on
   **703 (75%)**.
2. Within those 703, detection depends on a randomly mutated literal happening
   to produce a failure whose trace is ≥0.8 similar to the original.

The dominant cause of vacuity — 104 of 235, summing mechanisms 1 and 5 — is
**structural**: FixCheck only understands self-contained tests, with literals, in
their own class. That profile is a minority in Defects4J and, as the table above
shows, it is not distributed at random across projects. Any comparison of
overfitting rates **per project** inherits that bias and must declare it.
