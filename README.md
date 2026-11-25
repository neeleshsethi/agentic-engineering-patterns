# Agentic Engineering Patterns

Production engineering patterns for building reliable deep agents.

📖 **Read online:** https://neeleshsethi.github.io/agentic-engineering-patterns/

This repository documents real-world lessons learned while building production agentic systems with LangGraph, Python, DynamoDB, and Server-Sent Events (SSE). It is written for engineers and interns who can build ordinary APIs, but are new to the extra failure modes that appear when an agent can pause, resume, remember, stream, and call tools.

Unlike introductory tutorials, these articles focus on problems that do not crash your application. They quietly produce incorrect results while everything still looks healthy from the outside.

The running theme is simple: a deep agent is not just a prompt around a model. It is a distributed system with memory, checkpoints, human approval gates, locks, streams, and persistence boundaries.

## Article Series

The series follows the order you would actually **build** a production deep agent: two chapters that frame the system, eight build steps (with their deep dives as 7a and 8a–8e), two short bridge primers, and two capstones. The reading order lives in [`mkdocs.yml`](mkdocs.yml) and renders on the [home page](https://neeleshsethi.github.io/agentic-engineering-patterns/). Start at [Chapter 1](articles/ch1-problem-and-plan.md) and follow **Next**.

## Repository Structure

- `articles/` - Long-form technical blogs
- `examples/` - Minimal runnable code samples
- `docs/` - Architecture and design notes
- `diagrams/` - System diagrams

## Tech Stack

- LangGraph
- Python 3.11+
- DynamoDB
- Starlette
- Server-Sent Events
- asyncio

## Goal

Help engineers build production-ready agentic systems by documenting the kinds of failures that traditional tutorials rarely discuss.

## Learning Path For Python Interns

If you are new to deep agents, **read the [Article Series](#article-series) in order, starting at Chapter 1.** The goal is not to memorize LangGraph APIs. The goal is to build the production mental model: what state existed before this step, what state must exist after it, and what can go silently wrong at the boundary.

The `examples/` folders contain small before/after snippets for the same concepts. Read them when a production code sample in the long article feels too compressed. For each example, ask two questions: what state existed before this function ran, and what state must be true after it finishes?
