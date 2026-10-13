# Task — The Data World's Task-Synthesis Compiler

[中文](README.zh-CN.md) | **English**

`data_world` compiles a causal DAG into a data world; `task` compiles a data world into **analysis tasks with a rubric**.

It mirrors `dag_factory`: `dag_factory` is a **standalone upstream tool** connected to the main
pipeline only through `dag.json`; `task` is a **standalone downstream tool** connected to the main
pipeline only through `<world>/`. Neither enters the generation pipeline of `data_world`; both reuse
`data_world`'s modules (`dag` / `fs` / `sdk` / `envfile`) one-way, with no dependency in the reverse
direction.

```
dag_factory ──dag.json──▶ data_world ──<world>/──▶ task ──tasks.json──▶ harbor
```

## Usage

```bash
# Synthesize: world → tasks.json (the world directory is read-only, never modified)
python -m task synth --worlds ./outputs/worlds --out ./tasks -v

# Export: tasks.json → harbor benchmark task directories (grading runs inside the container)
python -m task export --tasks ./tasks --out ./benchmark
```

This package has two stages: `synth` performs task synthesis, and `export` exports Harbor benchmark
tasks (the in-container grading implementation is shipped into the task directory along with the
export). The resulting `tasks.json` can also be consumed by downstream evaluation tools.
`synth` supports resume: it skips task_ids already present in `tasks.json` (sampling is
deterministic, so a re-run lands on the same batch of targets); `--force` ignores existing artifacts
and re-synthesizes everything; `--task-id` synthesizes only the specified task (repeatable).

On shared machines, credentials should be injected through environment variables (the `ANTHROPIC_*`
variables in the project-root `.env`) or the runtime platform's secret mechanism; command-line key
arguments end up in shell history and should not be written into scripts.

## First Principles of Task Synthesis

Data analysis has **no** retrieval-style "multi-hop QA" kind of task. Given a target node on a DAG,
only two things can be asked:

- looking at "causes" along its **in-degree** — which factors drive or predict its changes;
- looking at "consequences" along its **out-degree** — which downstream outcomes its changes are associated with.

Sampling therefore has only one dimension: stratify by **topological depth** into K=4 strata, take
the Top-1 node by degree in that direction from each stratum, at most 2K=8 tasks per world.
Stratification prevents a global Top-K from degenerating into a hub preference; ties in degree are
broken by node order in `dag.json`, guaranteeing that repeated sampling lands on the same batch of
targets (the precondition for resume).

The synthesis agent's exploration is **completely unconstrained**: no prescribed workflow, no file
list, no statistical-method suggestions. It must first learn what the data actually looks like
(including its data flaws) before it can write verifiable evidence; a prescribed workflow would only
make it copy the workflow instead of verifying the data. There are only three hard constraints: the
delivery format, "conclusions must not leak", and **no numbered lists may appear in the task
prompt**.

The third rule is not a typographic preference: **the structure of the synthesis prompt gets treated
as a template for the task prompt**. Earlier, `[题面原则]` ("task-prompt principles") itself was a
numbered list from 1. to 5.; in practice, **26 of 36 tasks (72%)** had a task prompt that copied out
a numbered list, and the items in **26/26** of them were regurgitations of prompt wording
(`混淆` confounders / `伪线索` spurious clues / `join 键` join keys / `口径` metric definition /
`粒度` granularity / `方向` direction / `量级` magnitude / `异质性` heterogeneity / `局限`
limitations / `可行动` actionability) — even the `[工作方式]` ("working method") section's "don't
approach it with a prescribed workflow" and the dimension descriptions of the two rubric sections
were carried into the task prompt. **42 of them** named specific field identifiers from the rubric
(one task, `p000002/cause_3`, had 9: `event_type`/`issuing_body`/`latency_days`/…), effectively
handing the solver the solution outline together with which fields to look up — while `[题面原则]
1.` of that same prompt demanded exactly "not a step-by-step tutorial": the prompt contradicted
itself. The harm is threefold: the task prompt lays out the report skeleton for the solver, so it
can no longer test "judging for yourself what to look at"; `insight_discovery` points become easier
to earn, so the 26 tasks with numbered lists and the 10 without are **not on the same difficulty
scale**, and averaging them in one benchmark is meaningless; `check_no_leak` only asserts that the
task prompt contains no rubric **verbatim text**, which cannot stop this kind of "naming the same
batch of fields in different words" leakage.

The root cause is that **two audiences were mixed in the same passage**: instructions for the
synthesis agent and task-prompt content for the solver were not separated, and when the agent cannot
separate them it carries over both the structure and the vocabulary. The prompt is therefore split
into audience-specific sections (`[题面（task_prompt）怎么写]` "how to write the task prompt" /
`[rubric 怎么写]` "how to write the rubric"), switched entirely to unnumbered paragraphs, explicitly
forbids numbered lists in the task prompt, and declares after the rubric dimension descriptions that
"these dimension words must not be carried into the task prompt". The two rubric sections are also
always called by their real key names `data_computation` / `insight_discovery`: earlier they were
typeset with `A.`/`B.` labels, while the prose said "the rubric has only two keys, A and B" — but
there is no key named `A` in the JSON.

**The standard for the task prompt is the kind of short, quick, but unambiguous analysis commission
from everyday business.** Two to three hundred characters, one or two paragraphs, stating who you
are and the business question to be answered. This single standard explains both length and content,
so it replaces "don't leak details"-style enumerations: a real client only knows what they want to
figure out, not what pitfalls lie in the data — that is exactly why they hire an analyst — so they
could never say "note that the metric definition affects the conclusion" or "these two files are
joined on `customer_id`"; whereas the synthesis agent has just thoroughly explored the world, and
such sentences come to it effortlessly. Give the standard positively rather than listing
prohibitions negatively, and the model knows how to put pen to paper.

The longer the task prompt, the more inevitably it slides into paraphrasing data inventories,
data-flaw categories, join keys, or "note that something affects the conclusion"; and this kind of
leakage is **paraphrase, not verbatim copying**, so `check_no_leak` structurally cannot catch it (it
compares against rubric verbatim text) — it can only be constrained by the prompt. "Unambiguous"
must be explained separately from "listing items", otherwise the two get conflated: unambiguous
means the question itself has boundaries (which matter to answer, about whom, a business result
under what metric definition), whereas listing the deliverables as items hands over the report
skeleton.

**No items to fill in.** Earlier the task prompt was required to "state the identity and context,
what data is on hand, what to answer, and what to deliver" — a **content checklist** is the same
kind of thing as a numbered list, the synthesis agent followed it item by item, and in practice
produced 641 characters while pre-announcing data-flaw categories and join keys. For the same
reason, we no longer enumerate "don't list data inventories / don't pre-announce data flaws / don't
name join keys / don't hint at traps" one by one: these are all instances of the same mechanism —
giving the standard plus one counterexample generalizes, while a side-by-side list of prohibitions
itself demonstrates list form.

**The data directory is not written into the task prompt.** The data location is filled in by the
task-issuing side according to convention (data is agreed to live under `env/`); repeating it in the
task prompt is pure duplication, and mentioning the directory makes it easy to slip into describing
its contents.

**Resources must be named with absolute paths.** The copy is mounted into the session via
`--add-dir`, while the agent's cwd is an empty `run_dir` outside the copy — when the prompt writes
bare filenames ("feel free to audit WORLD.md, dag.json, .build/"), those names do not exist under
cwd. In practice, across the rubric evidence of 36 tasks, references to `WORLD.md` numbered
**0** and references to `.build/` only **4**; the agent basically only groped its way to `env/` via
`Glob`: the world manual and the canonical truth — the two resources that should most be used to
verify evidence — were in fact never opened. `build_synth_prompt` therefore **requires** the
absolute paths of `world_copy` / `run_dir` (no default values — silently degrading to bare filenames
is exactly this bug), and explicitly requires reading `WORLD.md` first to build a global
understanding.

## rubric: Two Sections

The section boundary is the **solving stage** — before analysis / during analysis:

| section | prefix | what it grades |
|---|---|---|
| `data_computation` | `dc-N` | Every step on the way from raw files to an analyzable table: files and fields, record granularity, join keys, date and encoding parsing, missing and duplicate handling, rows to drop or correct, row count after dedup and sorting, the metric definition of denominators. **The focus is preprocessing and cleaning** |
| `insight_discovery` | `id-N` | Everything done after the clean table is in hand, **including the computations within it**: group comparisons, correlation and lag, detrending, controlling confounders, ranking and heterogeneity, magnitude, counter-evidence/spurious clues, mechanism boundaries and limitations |

The criterion is a single sentence: was this number obtained while **organizing the data**, or while
**answering the question**? Do not split by "simple vs. profound" — described that way, the two
sections' vocabularies overlap (`关键数值` "key numbers" and `方向与量级` "direction and magnitude"
cannot divide a correlation coefficient between them), cleaning steps get squeezed out, and
statistics produced by analysis get classified into `data_computation` because they "feel technical
to compute". Correlation coefficients, regression slopes, quantile differences, and lag orders all
belong to `insight_discovery`.

Each scoring item has only two fields, `criterion` + `points` (`contract.ITEM_FIELDS`); the
validator rejects extra fields. There used to be an additional `evidence` field holding the
"judge-only gold standard", whose boundary with `criterion` is inherently blurry — both describe the
same fact, which side a verifiable number went to was entirely up to the synthesis agent's guess,
and what grading receives is the concatenated text, so splitting into two fields gave it no extra
information. After the merge there is a single rule: `criterion` must be self-contained enough to be
judged independently.

Points: ordinary facts 1–3 points, key facts at most 5 points; **negative-point items** −1..−3.

**A positive-point criterion must be both "necessary" and "certain".** The two gates block two
different kinds of bad scoring items; neither can be missing.

*Certain* governs "is this fact reliable": the synthesis agent has actually computed it under
several legitimate metric definitions, and every path reaches the same conclusion, so grading can
look only at the result, not the path. When a quantity depends on which path is taken (denominator
as people or person-months, whether inactive periods are included, whether outliers are
winsorized), no specific value is rewarded; instead, the path known to go astray is written as a
negative-point item — an uncertain quantity can still have its boundary judged with certainty. Only
a synthesis agent that has run the numbers can make this judgment; it cannot be templated, so the
prompt gives the reasoning, not a rule table.

*Necessary* governs "should this fact be tested": a positive-point criterion is either an
unavoidable step on the way to the answer, or a conclusion that must appear in the answer. The test
is a counterfactual question — remove it from the answer sheet; is the answer still complete?
Because reward's denominator is the sum of positive points, this gate directly determines reward's
calibration: every extra positive-point criterion the task prompt did not ask about lowers the
ceiling of an answer sheet that deserved full marks. After thoroughly exploring the world, the
synthesis agent inevitably runs into many true but unasked-for side discoveries (in practice, one
item — "the structure of sports disciplines is determined by club supply" — accounted for 2/36
points, while the task prompt only asked about three things: registration propensity, participation
depth, and retention); they are completely certain, so the certainty gate alone cannot block them.
The test must land on the task prompt, not on the synthesis agent's interests.

Necessary does **not** mean "leaves a trace in the report": deduplicating by primary key, reading
codes with leading zeros as strings — done right they leave no trace, done wrong everything
downstream is wrong; they are unavoidable steps and still earn positive points; the corresponding
negative-point item states which specific wrong number the wrong path yields, and the two do not
overlap. The prompt states this explicitly, otherwise "process details" get wholesale demoted to
negative points — and `data_computation` is exactly the process-dominated section, which after such
demotion would have nothing left but negative points.

The synthesis side and the grading side must be dual: if metric-definition-dependent values were
allowed as positive-point criteria, grading's rule that "values should agree with the criterion"
would mark wrong a solver who took an equally correct alternative path. Grading looks only at
results, not paths — and its precondition is exactly that positive-point criteria here are
path-independent. Change one side and you must change the other.

**The two sections together must have at least one positive-point item**, hard-validated by
`validate_task`. Reward's denominator is the sum of positive points across both sections; if all
items are negative the denominator is 0 and reward is identically 0 for any answer — validation
passes while grading silently fails. The constraint is global only; each section is not required to
have positive points: which section the certain facts concentrate in is a property of this world's
data, and forcing a `data_computation` section with extreme metric-definition divergence to scrape
together positive points is exactly what this rule is meant to avoid.

**Negative-point items do not form their own section**; they are classified into one of the two
sections above depending on whether they judge a data operation or an insight, distinguished only by
negative points. This must be guaranteed jointly by the schema example plus the validator — the
prompt alone cannot hold it: earlier the prompt said "in addition, be sure to write 2–4 more
negative-point items" while the schema gave only two positive examples, and models generally read
this as "open another section" — in practice, 24 of 35 tasks invented four section names of their
own, `negative` / `negative_scoring` / `negative_rubric` / `negative_items`, totaling 117
negative-point items, and since `build_rubric` only iterates over `SECTIONS`, these items **passed
validation and vanished at grading time** (87% of all negative-point items). Now `validate_task`
reports an error on any unknown section and feeds it back for retry.

Grading semantics (part of the artifact contract; downstream grading implementations should follow
the same semantics):

- a positive-point item scores when `status=satisfied`;
- a negative-point item deducts only when `status=triggered` (not being mentioned in the report does
  not count as triggered);
- `reward = clamp(total score, 0, positive cap) / positive cap`, always landing in [0,1]: triggered
  negative-point items can at most press it down to 0; negative reward never occurs.

The `dc-N`/`id-N` numbering is generated by `contract.build_rubric` and is the join key between
grading results and scoring items; **changing it breaks comparability with historical data**.

## Invariants

0. **The location of `env/` is resolved in one place.** `contract.resolve_env_dir` first looks for
   `<tasks>/<world>/world_copy/env` left behind by synthesis; if absent, it falls back to
   `<world_root>/<world>/env` pointed to by `--world-root`; the `WORLD_COPY` constant is also
   defined in `contract`, so synthesis (writing the copy) and export (fetching `env/`) share the
   same source. Large-scale synthesis generally uses `--no-keep-copy` to keep no copy (copies
   occupy disk linearly in the number of worlds); the artifacts then contain no `world_copy`, and
   `export` must be passed `--world-root` to fall back to the world directory — the copy is a
   byte-for-byte copy of the original world anyway, so the `env/` content is identical.
1. **The world directory is read-only, and this is a structural guarantee.** After synthesis copies
   a replica, it `chmod`s away the write bits, and the agent's working directory is outside the
   copy — writing to the world fails outright with `EACCES`. We do not rely on a sha256 manifest to
   detect contamination after the fact and then re-copy: detection can only find what has already
   happened; structure can keep it from happening. (Permission bits do not bind root; by design,
   synthesis runs as an ordinary user.)
2. **Grading semantics has exactly one implementation.** Grading is owned by `export/grade.py`; the
   harbor container verifier uses the same source code copied byte-for-byte into `tests/` at export
   time, and the grading prompt is defined once by `JUDGE_PROMPT` in `prompts.py`, from which a
   self-contained `judge_prompt.py` is generated at export time. No "one copy in the package, one in
   the container" evolving separately.
3. **The contract is owned by one place.** `contract.py` is used simultaneously by prompts
   (declaring the schema and delivery method handed to the agent), pipeline (validating artifacts),
   and export (`build_rubric` flattening, `check_no_leak` leakage prevention); there is no drift of
   the kind "the prompt asks for a file, the validator only accepts marker blocks".
4. **Credentials are read only from environment variables.** No token is ever written into the code;
   locally they come from the project-root `.env`. Harbor's evaluation platform has no runtime
   environment-variable injection channel, so judge credentials are written at export time into each
   task's `tests/judge.env` (task directories are created on disk with mode 0700), with a resolution
   order consistent with the in-container `judge.py` (`JUDGE_*` first, falling back to
   `ANTHROPIC_*`).
5. **All LLM calls are streaming.** Synthesis sessions use `--output-format stream-json`; the judge
   uses SSE. Timeout semantics are therefore **the gap between events**, not the total generation
   duration: as long as the model is still emitting tokens, it is not a timeout. The synthesis
   prompt therefore needs no "limit output length" self-restraint — the rubric gets as many items as
   it should.
6. **Leakage prevention is two independent channels, each plugged once.** On the task-prompt side,
   `contract.check_no_leak` asserts that no rubric conclusive text appears; on the image side, the
   whitelist only `COPY data/`, and asserts at build time that `WORLD.md` / `dag.json` / `.build`
   do not exist.

   The task-prompt-side criterion is "a fragment that is both a substring of the task prompt and
   contains a **conclusion marker** (numbers, magnitude words, causal or comparative words)", rather
   than the former "enough natural language remains after stripping identifiers". The length
   criterion fails in both directions: the old regex `[\w./{}(),：-]+`'s `\w` **includes CJK
   characters** under unicode, so "留存呈饱和曲线，12 个月后边际增益趋近 0" ("retention shows a
   saturation curve; marginal gain approaches 0 after 12 months") was stripped down to `，` (4
   characters) and exempted under the 8-character threshold — Chinese conclusion sentences copied
   verbatim into the task prompt were never reported; after changing to stripping ASCII only,
   `env/a.csv 与 env/b.csv 按 uid 关联` was left with "与按关联" ("with / by / join"), long enough
   that a task prompt legitimately naming files got false-positived. What distinguishes the two is
   not length but "whether it states what the result is". Numbers must be tested on the
   **unstripped** original text (the digits in `7500 行` "7500 rows" and `高 3.2 个百分点` "3.2
   percentage points higher" are the strongest signal), while digits immediately adjacent to letters
   in paths (`queue_metrics_5m.parquet`) are stripped along with the identifier. After `evidence`
   was merged, `criterion` carries both the metric definition and the conclusion, so this check is
   the only backstop. The word list is necessarily incomplete; the main line of defense remains the
   hard floor in the synthesis prompt.
7. **Retries are layered.** `runner` handles transient transport errors (`_TRANSIENT_RE`, default 10
   retries, exponential backoff starting at 20 seconds, at most 600 seconds per attempt);
   `pipeline` handles invalid artifacts and feeds the validation error text back into the next
   prompt. The two do not compensate for each other.
8. **Identity fields are injected one-way, without an agent round-trip.** The agent only delivers
   `task_prompt` + `rubric` (`contract.AGENT_FIELDS`); `task_id` / `task_kind` / `target_node` /
   `degree_kind` / `degree` are written directly by `contract.attach_identity` from
   `select_targets`' assignment (`contract.IDENTITY_FIELDS`). These are **our own bookkeeping**:
   `task_id` determines the directory name, the keys of `tasks.json`, and resume decisions; the copy
   the agent transcribed used to be compared against the assignment once and then discarded. Having
   it transcribe known constants only adds, out of thin air, a failure mode of "transcribe wrong and
   the whole artifact is retried" (in practice, 0 transcription errors across 36 tasks — i.e., this
   check never caught anything). After injection, the field set of `tasks.json` is **exactly the
   same** as old artifacts; only the filling method changed from "validate" to "inject", so it is
   backward compatible with historical artifacts.

## Directory Structure

```
task/
  contract.py   Artifact contract: schema, delivery markers, validation, rubric flattening, leakage check
  runner.py     Streaming agent runner (streaming replacement for ClaudeRunner)
  cli.py        Subcommands + error boundaries
  synth/        Task synthesis
    sampler.py    Stratified target-node selection (reuses data_world.dag's validation and normalization)
    prompts.py    Synthesis prompts
    pipeline.py   Orchestration: read-only copy, validation retry, resume, aggregation
  export/       Harbor export
    harbor.py     Exporter: tasks.json → harbor task directories
    grade.py      The single grading implementation + container verifier entry
    judge.py      Judge client (streaming SSE, zero third-party dependencies)
    prompts.py    JUDGE_PROMPT (generates a self-contained judge_prompt.py at export time)
```

`contract.py` and `runner.py` stay at the package root: the former is the single definition site of
the artifact contract (prompts declare, pipeline validates, and export consumes by the same
semantics), the latter is shared by synthesis sessions and the CLI.

`grade.py` / `judge.py` / `prompts.py` in `export/` are the grading assets shipped with the
container: they are copied byte-for-byte into the task directory's `tests/` at export time
(`judge_prompt.py` is generated from `JUDGE_PROMPT`); container reward must come from this very
source code to be comparable — this is how invariant 2 is implemented.

| module | calls processes/network? |
|---|---|
| `contract` / `synth.sampler` / `synth.prompts` / `export.prompts` / `export.harbor` | no |
| `export.grade` | no (judge is injected by the caller) |
| `export.judge` / `runner` | yes |
| `synth.pipeline` | yes (only through the injected runner) |

## Regression Evidence

- `sampler` and the historical implementation were compared on target selection across 339 generated
  worlds (fields such as `task_id` / `target` / `degree` / `stratum`), with zero differences;
- read-only copy: all 8 write operations across the Python and Bash paths were denied; reads and
  directory listings are unaffected.
