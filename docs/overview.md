# MonOSS Project Overview

## Purpose

MonOSS (Monist Organization and Simulation System) is a personal information management platform centered around the idea that every piece of information can be represented as an object with properties and relationships.

Unlike traditional note-taking or task-management software, MonOSS does not separate information into rigid categories such as notes, contacts, files, projects, calendars, or tasks. Instead, everything is modeled as a single fundamental object type called a **mote**. Specialized behavior emerges from the properties attached to motes and the relationships between them.

The goal is to create a flexible knowledge graph that more closely represents how real-world information is connected than conventional folder hierarchies or isolated applications.

---

## Philosophy

MonOSS is built around several core principles.

### Everything is a mote

Every entity in the system is represented by a mote.

Examples include:

* people
* projects
* tasks
* meetings
* ideas
* books
* documents
* locations
* organizations
* events

The system should never introduce separate object models for concepts that can instead be represented as motes.

---

### Relationships are first-class

Connections between objects are just as important as the objects themselves.

Examples:

* Alice **works_on** Reactor Project
* Reactor Project **contains** Fuel Analysis
* Meeting **references** Design Document
* Book **written_by** Author

Relationships are stored explicitly and should never be duplicated as hidden references inside individual objects.

---

### Properties describe, relationships connect

A property is data owned by a single mote.

Examples:

* name
* description
* due date
* phone number
* priority
* creation time

Relationships connect multiple motes together.

Maintaining this distinction is fundamental to the data model.

---

### Extensibility over specialization

The system should avoid hardcoding behavior for specific object types whenever possible.

New object types should typically be created through data, configuration, or plugins rather than modifications to the core architecture.

---

### Simple core, powerful composition

The core system should remain small.

Complex behavior should emerge by composing:

* motes
* properties
* relationships
* rules
* plugins
* services

Avoid adding special cases that only solve one problem.

---

## Long-Term Goals

MonOSS is intended to become a unified platform for:

* personal knowledge management
* task management
* project planning
* note organization
* contact management
* research organization
* simulation and rule execution
* automation
* plugin-based extensions

Rather than replacing one application, MonOSS aims to replace the need for many disconnected applications by providing a common object model.

---

## Current Architecture

The current implementation is written in Python.

Data is stored in SQLite.

The application follows a layered architecture that separates:

* data storage
* repositories
* services
* business logic
* user interfaces

The project intentionally favors readability, maintainability, and correctness over premature optimization.

---

## Design Priorities

When making implementation decisions, prioritize the following in order:

1. Correctness
2. Simplicity
3. Maintainability
4. Extensibility
5. Performance

Avoid introducing unnecessary abstractions, frameworks, or dependencies.

---

## What Claude Should Optimize For

When implementing features, optimize for consistency with the existing architecture rather than minimizing lines of code.

New features should:

* reuse existing abstractions whenever practical
* preserve the "everything is a mote" philosophy
* avoid duplicating data
* keep APIs predictable
* maintain backwards compatibility when reasonable
* include tests
* include documentation for significant architectural changes

Do not redesign the architecture unless explicitly instructed.

If requirements are ambiguous, prefer asking for clarification over inventing behavior.

The objective is not simply to produce working code. The objective is to build a coherent, long-lived system whose architecture remains understandable as it grows.
