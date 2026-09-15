# Patches to FixCheck

`fixcheck/` is a gitignored clone of
[facumolina/fixcheck](https://github.com/facumolina/fixcheck) (HEAD `9503ba2`).
`scripts/buildFixcheck.sh` applies these patches in order after every fresh
clone, runs FixCheck's unit tests and builds `build/libs/fixcheck-all-1.0.0.jar`.
**Never edit `fixcheck/` without regenerating the patch it belongs to**: the
clone is disposable.

Each patch is self-contained and comes with its own JUnit test, so any of them
can be proposed upstream on its own. Every new property is optional and
defaults to FixCheck's previous behaviour.

Figures quoted below are from this project's archived campaign
(`results/old/9-Sep/`: 767 analysed test classes, 6698 failing prefixes); the
investigation is written up in `notes/informe-fixcheck-reunion.md` and
`docs/fixcheck-verdict-limitations.md`.

| Patch | Fixes | New properties |
|---|---|---|
| 0001 previous-assertion-keeps-assertions | `previous-assertion` stripped the assertions it was meant to keep | -- |
| 0002 similarity-trace-normalization-jdk9 | trace normalization dead on Java 9+ | -- |
| 0003 generic-ollama-assertion-generator | assertion generation with any Ollama model | `assertion-generator=ollama:<model>[@[<host>:]<port>]`, `ollama-model`, `ollama-host`, `ollama-port`, `ollama-timeout-seconds` |
| 0004 relocate-dependencies | bundled libraries shadowed the subject | -- |
| 0005 subject-classloader | `IllegalAccessError` on package-private/protected members | `subject-classpath` |
| 0006 run-mutated-method-only | JUnit 3 classes ran every sibling test | -- |
| 0007 non-compiling-continues | one non-compiling prefix aborted the run | -- |
| 0008 prefix-timeout | one never-ending prefix stalled the run | `prefix-timeout-seconds` |
| 0009 seed | runs could not be repeated | `seed` |
| 0010 output-dir-and-ollama-options | output tied to the working directory; LLM assertions not repeatable | `output-dir`, `ollama-temperature`, `ollama-seed` |
| 0011 qualified-testsuite | rewritten `suite()` referenced an unimported `TestSuite` | -- |
| 0012 assertion-generation-failure-continues | one failed or timed-out assertion-generator call aborted the run | -- |

## 0001 -- `previous-assertion` keeps the original assertions

`InputTransformer` skipped assertion removal when the generator was
`"previous-assertion"`, but `FixCheckProperties.ASSERTION_GENERATOR` holds the
resolved class name, so the guard never matched and every prefix lost its
assertions (`UsePreviousAssertGenerator` does not re-add them). A regression
from upstream commit `8f05d54` (2024-06-26), which replaced option keys by class
names; the July 2023 code the paper's experiments ran on compared keys and
worked. The guard now compares against `UsePreviousAssertGenerator`'s class name.

## 0002 -- Stack-trace normalization on Java 9+

`FailureChecker.normalizeFailureTrace` cut traces at
`sun.reflect.NativeMethodAccessorImpl.invoke0`, a frame renamed
`jdk.internal.reflect...` in Java 9, so nothing was cut and runner frames
(Ant for the original failure, JUnitCore for the prefix) dominated the
Levenshtein distance. It now recognizes both names and normalizes the original
trace too. Measured on Math 69: 0.474 -> 0.828 for the same assertion failure.

## 0003 -- Generic Ollama assertion generator

`CodeLlamaOllama` and `Llama3_1Ollama` hardcode their model and
`localhost:11434`. `OllamaGenerator` takes both from the configuration and asks
for bare assertion statements, which reasoning models otherwise answer by
re-emitting the whole method with variables the prefix does not declare.

## 0004 -- Relocate bundled dependencies

`fixcheck-all` carried commons-cli 1.6.0, commons-lang3 3.8.1,
commons-collections 3.2.2/4, guava, javaparser... under their own package names.
Put before a subject on the class path (as FixCheck's README does), FixCheck's
copy won: for Defects4J's Cli, Lang and Collections the prefixes ran against the
released library instead of the patched program (81 of 767 analysed classes;
Cli 35's prefix traces show `DefaultParser.java:385` where the checkout's line is
398), and 25 prefixes did not compile against the older APIs. The hand-rolled
`shadowJar` task is replaced by the Shadow plugin, relocating everything under
`org.imdea.fixcheck.shaded` except JUnit/hamcrest (the API shared with the tests
it runs) and `de.kherud.llama` (JNI, bound by name). `buildFixcheck.sh` fails if
an unrelocated class is left.

## 0005 -- One class loader for the prefix and the subject

The JVM puts two classes in the same runtime package only when one loader
defines both. The subject came from the application loader and each prefix from
`InMemoryClassLoader`, so any access to a package-private or protected member
raised `IllegalAccessError`: **2838 of 6698 failing prefixes (42%)**, and in 153
of 589 analysed runs every failure was one. With `subject-classpath` set, the
subject is no longer on `java -cp`; each prefix gets a fresh child-first
`SubjectClassLoader` over that class path that also defines the in-memory
classes, delegating only the JDK, JUnit and hamcrest to the parent. This also
removes shadowing and keeps static state from leaking between prefixes.
Test: `SubjectClassLoaderTest` reproduces the error with the old layout.

## 0006 -- Run only the mutated method

`PrefixRunner` filtered by method only when it carried `@Test`; JUnit 3 test
cases ran the whole copied class (482 of 767 classes). Sibling tests then ran
on their original data, and a regression the patch caused anywhere in the class
fed the similarity score: in 1001 failing prefixes only siblings failed, and a
flag on JacksonDatabind 6 came entirely from one. The request is now always
filtered by method name (parameterized runs `method[i]` kept), for JUnit 3
`TestCase`s with or without `suite()` as well.

## 0007 -- A prefix that does not compile no longer aborts the run

A prefix that failed to compile left a null result, and
`FixCheck.generateSimilarPrefixes` dereferenced it: a `NullPointerException`
ended the run and discarded every prefix generated so far (64 test classes).
It is now recorded as non-compiling and generation goes on.

## 0008 -- Per-prefix timeout

A mutated literal can turn a size or a loop bound into something that never
ends -- Math 10's derivation order went from 2 to 86 -- and one such prefix
stalled FixCheck until an external timeout killed it with nothing written.
With `prefix-timeout-seconds`, each run happens on its own thread; one that
outlives the budget is stopped and recorded as timed out: neither passing nor
failing, not scored, counted in the new `timed_out_prefixes` column of
`report.csv` (appended last) and saved under `timed-out-tests/`.

## 0009 -- Seed

Every call site drew from its own `new Random()`, so the same subject gave
different prefixes on every run (4 passing / 1 crashing, then 2 / 3). All
choices now come from one `RandomSource`, seeded by `seed`.

## 0010 -- Output directory and Ollama sampling options

- `output-dir`: where `report.csv` and the prefixes go (default
  `fixcheck-output`, relative to the working directory, as before). Running from
  a scratch directory to keep outputs apart broke every test that opens files
  relative to its project root (140 prefixes of Compress); FixCheck can now run
  from the subject's root with its output elsewhere.
- `ollama-temperature`, `ollama-seed`: sent as the request's `options` only
  when set, so the same prefix gets the same assertions. No `num_ctx` is sent,
  so the model stays loaded with the daemon's context length.

## 0011 -- Fully qualified `TestSuite` in the rewritten `suite()`

`TransformationHelper` renames the copied test class and replaces the body of
its `suite()` method with `return new TestSuite(<NewName>.class)`, without
adding an import. Test classes that build their suite another way and never
import `TestSuite` -- commons-collections' `BulkTest.makeSuite` -- then produced
prefixes that could not compile (`cannot find symbol: class TestSuite`, 10 of
Collections' test classes, each of which aborted its run before 0007). The body
now names `junit.framework.TestSuite` in full.

## 0012 -- A failed assertion-generator call no longer aborts the run

`generateAssertions` was called unguarded, and `OllamaGenerator` throws when a
call fails or outlives `ollama-timeout-seconds`: the exception left `main` and
the run ended with no report, discarding every prefix generated so far -- the
same failure 0007 fixed for non-compiling prefixes. Reasoning models make it
likely: in the archived campaign qwen3.6:35b took 17 s per call at the median,
51 s at the 90th percentile and up to 135 s (it thinks for ~5000 tokens before
writing one assertion). A prefix whose generator throws is now recorded as such:
it ran only without assertions, so it is neither passing nor failing, is not
scored, is counted in the new `assertion_generation_failed_prefixes` column of
`report.csv` (appended last) and saved under
`assertion-generation-failed-tests/`. Test: `FixCheckTest`.

## Regenerating a patch

The clone keeps each applied patch staged, so the working tree diff is always
the patch being written:

```bash
git -C fixcheck status            # earlier patches staged, nothing unstaged
# ...edit fixcheck/...
git -C fixcheck add -N .          # include new files in the diff
git -C fixcheck diff > scripts/fixcheck-patches/00NN-name.patch
git -C fixcheck add -A
```

Before committing, check the whole series applies to a pristine clone
(`bash scripts/buildFixcheck.sh` on a fresh `fixcheck/`).
