# core/submit.py
"""Submit: freeze a project's document as a numbered revision.

A submission is a mote in the project's own garden carrying the reserved
`submission` property -- the `.tex`, the PDF, any figure files (base64, the
same seam `core/images.py` uses) and the values the document quoted. So it
syncs, exports and undoes like everything else, and nothing about it is
special storage. The project's owner mote, in the parent garden, gets the
cover's `revision` and `status: submitted`.

Decided 2026-10-05 (`roadmap.md` -> *Output*): revisions count `r1`, `r2`,
... per project; with no note named, the garden's one root note (the one no
other note `\\include`s) is the document; and a garden with a failed check,
an unresolved macro or a broken model is refused unless forced, in which
case the submission records `forced: true`.
"""
import base64
import datetime
import sqlite3
from dataclasses import dataclass

from core import calc, document as document_repo, modoc, projects, simulation, texexport, undo
from core import tectonic
from core.projects import SUBMISSION_KEY
from core.motes import create_mote, delete_property, get_mote, list_motes, update_mote

SUBMITTED = "submitted"
SUBMITTED_ON_KEY = "submitted"

TEX_NAME = "main.tex"
PDF_NAME = "main.pdf"


class SubmitError(ValueError):
    """Nothing to submit, or an ambiguous request. The message is the
    sentence a surface shows as-is."""


class SubmitRefused(SubmitError):
    """The garden is not clean. `problems` lists why; `--force` freezes
    anyway."""
    def __init__(self, problems: list[str]) -> None:
        super().__init__(
            "not submitting; " + f"{len(problems)} problem{'' if len(problems) == 1 else 's'}:\n  "
            + "\n  ".join(problems) + "\nfix them, or pass --force to freeze anyway")
        self.problems = problems


@dataclass(frozen=True)
class Submission:
    mote_id: str
    revision: str
    date: str
    note: str
    forced: bool
    values: dict
    files: dict[str, bytes]

    @property
    def pdf(self) -> bytes | None:
        return self.files.get(PDF_NAME)

    @property
    def tex(self) -> str:
        return self.files[TEX_NAME].decode("utf-8")


def _submission(mote) -> Submission:
    spec = mote.properties[SUBMISSION_KEY]
    return Submission(
        mote_id=mote.id, revision=spec.get("revision", ""), date=spec.get("date", ""),
        note=spec.get("note", ""), forced=bool(spec.get("forced")),
        values=dict(spec.get("values", {})),
        files={path: base64.b64decode(data) for path, data in spec.get("files", {}).items()},
    )


def revisions(conn: sqlite3.Connection) -> list[Submission]:
    """Every submission in the garden, oldest first."""
    found = [_submission(m) for m in list_motes(conn)
             if isinstance(m.properties.get(SUBMISSION_KEY), dict)]
    return sorted(found, key=lambda s: _number(s.revision))


def _number(revision: str) -> int:
    digits = revision.lstrip("r")
    return int(digits) if digits.isdigit() else 0


def find(conn: sqlite3.Connection, revision: str) -> Submission:
    """A revision by label (`r2` or `2`)."""
    wanted = _number(revision)
    for s in revisions(conn):
        if _number(s.revision) == wanted:
            return s
    raise SubmitError(f"no revision {revision!r}; have: "
                      + (", ".join(s.revision for s in revisions(conn)) or "none"))


def root_note(conn: sqlite3.Connection, ref: str | None = None) -> str:
    """The id of the note to render: the one named, else the garden's only
    root note. More than one root is ambiguous and asks, never guesses."""
    notes = [m for m in list_motes(conn) if modoc.has_modoc(m.properties)]
    if ref:
        matches = [m for m in notes if m.id == ref or m.properties.get("name") == ref]
        if len(matches) != 1:
            raise SubmitError(f"no note named {ref!r}" if not matches
                              else f"{len(matches)} notes are named {ref!r}; use an id")
        return matches[0].id
    if not notes:
        raise SubmitError("this garden has no note to export -- write one with `edit`")
    included: set[str] = set()
    names = {m.properties.get("name"): m.id for m in notes}
    for m in notes:
        for node in modoc.includes(modoc.parse(modoc.read_source(conn, m.id))):
            included.add(names.get(node.ref) or node.ref)
    roots = [m for m in notes if m.id not in included]
    if len(roots) == 1:
        return roots[0].id
    raise SubmitError(
        "which note? " + ", ".join(repr(m.properties.get("name", m.id)) for m in (roots or notes))
        + " -- name one")


def problems_of(conn: sqlite3.Connection, bundle: texexport.TexBundle) -> list[str]:
    """Everything that should stop a submission: macros that did not
    resolve, a check that failed, a model with a unit or structure fault."""
    found = list(bundle.problems)
    for result in calc.checks(conn):
        if not result.passed:
            found.append(f"check {result.status}: {result.label}"
                         + (f" ({result.error or result.detail})" if result.error or result.detail else ""))
    found.extend(f"units: {p}" for p in simulation.check_garden(conn).problems)
    return found


def _values(conn: sqlite3.Connection) -> dict[str, str]:
    """What the document quotes, frozen for diffing: every given and answer."""
    rows = list(calc.givens(conn)) + list(calc.answers(conn))
    return {row.label: row.text() for row in rows}


def export(conn: sqlite3.Connection, garden: str, note_ref: str | None = None) -> texexport.TexBundle:
    """The document as `.tex` without freezing anything: the cover is the
    owner's as it stands, so a draft export shows the revision last
    submitted."""
    owner = projects.owner_of(garden)
    cover = owner.cover if owner is not None else None
    return texexport.export(conn, root_note(conn, note_ref), cover=cover)


def pdf(conn: sqlite3.Connection, garden: str, note_ref: str | None = None, fetch=None) -> tuple[bytes, texexport.TexBundle]:
    """A draft PDF: `export`, compiled. Nothing is written."""
    bundle = export(conn, garden, note_ref)
    return tectonic.compile_pdf(bundle.tex, bundle.files, fetch=fetch), bundle


def submit(
    conn: sqlite3.Connection, garden: str, note_ref: str | None = None, force: bool = False,
    fetch=None, today: datetime.date | None = None,
) -> tuple[Submission, undo.Command]:
    """Freeze the document as the next revision. Returns it and the undo
    step that removes the submission and restores the owner's cover."""
    projects.ensure_writable(garden)
    note_id = root_note(conn, note_ref)
    owner = projects.owner_of(garden)
    number = len(revisions(conn)) + 1
    while any(_number(s.revision) == number for s in revisions(conn)):
        number += 1
    label = f"r{number}"
    cover = None
    if owner is not None:
        cover = projects.cover_of({**owner.properties, "revision": label}, owner.mote_id)
    bundle = texexport.export(conn, note_id, cover=cover)
    found = problems_of(conn, bundle)
    if found and not force:
        raise SubmitRefused(found)
    pdf = tectonic.compile_pdf(bundle.tex, bundle.files, fetch=fetch)

    date = (today or datetime.date.today()).isoformat()
    files = {TEX_NAME: bundle.tex.encode("utf-8"), PDF_NAME: pdf, **bundle.files}
    spec = {
        "revision": label, "date": date, "note": note_id, "forced": bool(found),
        "values": _values(conn),
        "files": {path: base64.b64encode(data).decode("ascii") for path, data in files.items()},
    }
    note = get_mote(conn, note_id)
    mote_id = create_mote(conn, {"name": f"{note.properties.get('name', 'note')} {label}", SUBMISSION_KEY: spec})
    created = undo.for_create_mote(conn, mote_id, get_mote(conn, mote_id).properties, f"submit {label}")
    commands = [created]
    if owner is not None:
        commands.append(_stamp_owner(owner, label, date))
    submission = _submission(get_mote(conn, mote_id))
    return submission, undo.combine(f"submit {label}", commands)


_OWNER_KEYS = ("status", "revision", SUBMITTED_ON_KEY)


def _stamp_owner(owner: projects.Owner, label: str, date: str) -> undo.Command:
    """Write the cover on the owner mote, in the owner's garden. The undo
    reopens that garden: a Command's closures cannot assume the connection
    they were made on is the owner's."""
    from core import db
    before = {k: owner.properties[k] for k in _OWNER_KEYS if k in owner.properties}
    after = {"status": SUBMITTED, "revision": label, SUBMITTED_ON_KEY: date}

    def write(values: dict, remove: tuple = ()) -> None:
        owner_conn = db.get_connection(owner.garden)
        try:
            if values:
                update_mote(owner_conn, owner.mote_id, values)
            for key in remove:
                delete_property(owner_conn, owner.mote_id, key)
        finally:
            owner_conn.close()

    write(after)
    absent = tuple(k for k in _OWNER_KEYS if k not in before)
    return undo.Command(
        label="stamp cover", undo=lambda: write(before, absent), redo=lambda: write(after))


# --- revision diff ---------------------------------------------------

@dataclass(frozen=True)
class Change:
    ref: str
    before: str | None
    after: str | None

    @property
    def kind(self) -> str:
        if self.before is None:
            return "added"
        if self.after is None:
            return "removed"
        return "changed"


def diff(a: Submission, b: Submission) -> list[Change]:
    """Values that differ between two revisions, by name. Equal values are
    not listed -- a diff of a hundred unchanged numbers helps nobody."""
    out = []
    for ref in sorted(set(a.values) | set(b.values)):
        before, after = a.values.get(ref), b.values.get(ref)
        if before != after:
            out.append(Change(ref, before, after))
    return out


# --- the console's verbs -----------------------------------------------

OUTPUT_VERBS = ("export", "submit", "revisions")


def run_verb(conn: sqlite3.Connection, garden: str, verb: str, args: tuple[str, ...], fetch=None):
    """`export tex|pdf [path] [--note N]`, `submit [--force] [--note N]`,
    `revisions [diff ‹a› ‹b› | export ‹rN› [path] [--tex]]`. Returns the
    log line, how many motes were written, and the undo step (or None).
    Raises SubmitError (and tectonic.TectonicError) with the sentence to show."""
    from pathlib import Path
    from core.reserved import RESERVED_VERBS
    usage = f"usage: {RESERVED_VERBS[verb]['usage']}"
    words, flags = [], {}
    items = iter(args)
    for word in items:
        if word == "--note":
            flags["note"] = next(items, None)
            if flags["note"] is None:
                raise SubmitError(usage)
        elif word in ("--force", "--tex"):
            flags[word[2:]] = True
        elif word.startswith("--"):
            raise SubmitError(usage)
        else:
            words.append(word)
    note = flags.get("note")

    if verb == "export":
        if not words or words[0] not in ("tex", "pdf") or len(words) > 2:
            raise SubmitError(usage)
        path = Path(words[1] if len(words) == 2 else f"{garden}.{words[0]}").expanduser()
        if words[0] == "tex":
            bundle = export(conn, garden, note)
            path.write_text(bundle.tex, encoding="utf-8")
            for relative, data in bundle.files.items():
                (path.parent / relative).parent.mkdir(parents=True, exist_ok=True)
                (path.parent / relative).write_bytes(data)
        else:
            data, bundle = pdf(conn, garden, note, fetch=fetch)
            path.write_bytes(data)
        warn = f"; {len(bundle.problems)} problem(s): {bundle.problems[0]}" if bundle.problems else ""
        return f"wrote {path}{warn}", 0, None

    if verb == "submit":
        if words:
            raise SubmitError(usage)
        frozen, command = submit(conn, garden, note, bool(flags.get("force")), fetch=fetch)
        return (f"submitted {frozen.revision}{' (forced)' if frozen.forced else ''}: "
                f"{len(frozen.values)} values frozen -- `revisions export {frozen.revision}` "
                f"writes the PDF"), 1, command

    if not words:
        found = revisions(conn)
        return ("; ".join(f"{r.revision} {r.date}{' forced' if r.forced else ''}" for r in found)
                or "no revisions yet -- `submit` freezes one"), 0, None
    if words[0] == "diff" and len(words) == 3:
        changes = diff(find(conn, words[1]), find(conn, words[2]))
        return ("; ".join(f"{c.ref}: {c.before or '-'} -> {c.after or '-'}" for c in changes)
                or "no values changed"), 0, None
    if words[0] == "export" and 2 <= len(words) <= 3:
        found = find(conn, words[1])
        name = TEX_NAME if flags.get("tex") else PDF_NAME
        path = Path(words[2] if len(words) == 3 else f"{found.revision}{Path(name).suffix}").expanduser()
        path.write_bytes(found.files[name])
        return f"wrote {path}", 0, None
    raise SubmitError(usage)
