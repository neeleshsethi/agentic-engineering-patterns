---
summary: "What an evaluation measures, and the ways a passing score can still be wrong."
---

# Bridge · Evaluating an agent

Every page so far tested the agent the way you test a web API. You call it, check that nothing threw, and check that the right rows landed in the right table. That catches infrastructure bugs. It cannot tell you whether the agent picked the right data source or answered the question the user asked. For that you need an evaluation. Capstone 2 assumes you know what one is and how one can mislead you. This page covers that much.

The running example is the same retail analytics agent. A user asks a question about BRAND_A, the agent plans which of SOURCE_A through SOURCE_D to query, and it answers with figures from the rows it got back.

## Transport success is not task success

The series turns on one distinction, [transport success versus semantic success](glossary.md#transport-success-vs-semantic-success). A request that finished with `200 OK` has transport success. A run where the right context, state and plan held has semantic success.

Evaluation applies the same split to the agent's output. The run finished, the tool returned rows, and the answer streamed. All of that is transport success, and your logs and tests already see it. Task success means the user got what they asked for, built from the rows the agent retrieved and not from the model's memory. The series calls that rule [grounding](glossary.md#grounding). An agent that queries SOURCE_A when the question needed SOURCE_C has transport success and fails the task. So does an agent that writes a paragraph about a table instead of returning the table.

Nothing in the infrastructure can see that second kind of failure. Only an evaluation can.

## What an eval is

An evaluation, or eval, has three parts.

**A dataset of cases.** Each case is an input plus a statement of what good output looks like. The input is often a question and some seeded conversation state. Each case has a stable id, so you can find the same case in next week's run.

```python
case = {
    "id": "brand_a_canada_q3",
    "question": "BRAND_A sales by market in Canada, last quarter",
    "expected_sources": {"SOURCE_C"},
    "expected_behavior": "answer",      # or "clarify" when the question is ambiguous
}
```

**A grader.** A grader is a function that takes one case and the agent's output and returns a verdict. The verdict can be pass or fail, a number, or a label such as `correct`, `incorrect` or `clarify`.

**A score.** The score summarizes the verdicts across the dataset, for example "82% correct."

The dataset says what you care about, and the grader says how you check it. The score is the part people read, and it is also the part with the least information in it.

## Two kinds of grader

**Code-based graders** check something a program can decide. Did the agent choose exactly the expected sources? Does every figure in the answer appear in the rows it retrieved? Did it return a table when one was asked for? These graders are cheap and fast, and they give the same verdict every time. They can only check what you can state precisely.

```python
def grade_sources(case, run) -> str:
    return "correct" if run.sources_called == case["expected_sources"] else "incorrect"
```

**LLM-as-judge graders** hand the case, the agent's output and a rubric to a second model and ask it for a verdict. Use one when the property is real but hard to write as code. Examples are whether the answer addresses the question that was asked, or whether it says plainly that SOURCE_B has no Canada coverage. A judge can read meaning, which code cannot. It also has its own failure modes. Its verdict can change between runs. It can favor answers that are long, fluent or confident. And it grades only what you show it. A judge that sees the answer but not the retrieved rows cannot tell whether a figure was read from data or made up. It will often pass a plausible-looking number.

Use code where the check can be stated exactly, and a judge only for what is left. Before you trust a judge, read a sample of its verdicts next to the outputs it graded, so you know it checks the thing you meant.

## What a score can hide

A score is an average, and an average throws information away. Four kinds of loss come up again and again.

**The average hides a category.** Say the dataset has 80 single-market questions and 20 cross-market questions. A change lifts single-market accuracy from 85% to 95% and drops cross-market accuracy from 70% to 40%. The overall score rises from 82% to 84%. It reports a small win and hides a large regression in one slice of the dataset.

**The score grades a decision but not the artifact.** A grader that checks "did the agent call SOURCE_C?" says nothing about whether the answer used SOURCE_C's rows correctly. A high score on the decision says little about the output.

**The verdict buckets hide a tradeoff.** If `clarify` and `incorrect` both count as "not pass," then an agent that stops asking questions and starts guessing can raise the pass rate. Some of those guesses are right, and the rest turn into confident wrong answers. The pass rate went up while the agent got less honest. You only see this if you keep the verdicts apart.

**The two runs did not test the same cases.** If cases were added or removed between runs, or expected labels changed, the two scores came from different datasets. Comparing them tells you about the datasets as much as the agent.

A judge can add a fifth problem on top of these. It can grade the wrong property consistently, and the score will look stable and trustworthy while measuring something nobody asked for.

## The minimum evaluation loop

A small loop beats a large dataset that nobody reads. For an agent, the minimum is:

1. **Fix the dataset.** Version it, give each case a stable id, and record where each expected label came from.
2. **Run every case, more than once if you can.** The same agent can answer the same question two ways, so a single run per case mixes noise with signal.
3. **Grade each case and keep the per-case verdicts.** Store `case_id -> verdict`, not just the mean. Every analysis below needs the per-case rows.
4. **Compare against a baseline on the same cases.** Restrict to cases present in both runs with unchanged labels. Count how many moved from each verdict to each other verdict, rather than subtracting two averages.
5. **Read the failures.** Open the transcripts of the cases that changed. The transcripts show why a verdict changed, and the score cannot.
6. **Add a case for every production failure.** When a user hits a bug, turn it into a case before you fix it. If the bug comes back, that case fails.

Steps 3 and 4 are the ones teams skip, and they are what Capstone 2 is about.

## What Capstone 2 builds on this

[Capstone 2 · When the score passes and the work fails](capstone-2-when-the-score-passes.md) takes these ideas into a real system. Part 1 lists nine failures where the tool succeeded and the task did not. Part 2 grades decisions and artifacts separately, and pairs cases across two snapshots. It then builds the verdict transition table from step 4 and finds a drop in clarifying questions that turned into confident wrong answers. It also checks where the labels came from and whether the holdout set was really held out. Part 3 shows how to design an experiment when you want to claim that one specific change caused an improvement.

*Next: [Capstone 2 · When the score passes and the work fails](capstone-2-when-the-score-passes.md).*
