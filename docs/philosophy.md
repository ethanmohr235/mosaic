# Philosophy

Mosaic (formerly MonOSS, still the name in code) is where an engineer's
projects live: the model, the calculations, the document that shows them,
and the schedule they're due on.

Mosaic is an object graph.

Everything in Mosaic is a mote.

Motes have properties. These properties could be anything (text, dates,
numbers, etc.)

Relationships can exist between motes, organizing them.

The system should model reality, not files.

It should favor extensibility over convenience.

It should avoid special cases. Prioritize minimalism and elegance.

## No mote is higher class

A deadline, a rule, a note, and a person are the same kind of thing, and
the core never treats one as more important than another. In practice,
calculation is where the real work happens, so that's where most of the
investment goes.

## Editors are shortcuts over the graph

Non-graph views are core to the app: worksheets, tables, calendars,
documents, plots. Each one is a faster, more intuitive way to read and
write motes and relationships, never a separate store.

Features may hide plumbing. They may not replace it. Whatever a view
creates must be ordinary motes and relationships that an advanced user can
open up and edit directly.

## Convention, not schema

The core stores motes, relationships, and gardens, and nothing more
opinionated. Apps may share a small set of conventions (for example, a mote
may own a garden) and build features on them. Every such feature must still
work when the convention is absent. The user arranges their own work, and
the apps make the common arrangement fast.

## Organization falls out of work

Any feature that asks the user to file things will die. Portals, folders,
and the library were all removed for that reason. Structure has to come
from something the user was already doing, like starting work on a
deadline, and never from a separate act of filing.

## The feature test

Does it shorten the path from a problem to finished work, or make that
work more trustworthy? If not, cut it.
