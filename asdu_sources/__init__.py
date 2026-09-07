"""Built-in readers. Adding a source requires an explicit registry entry."""

from . import claude, codex, omp

READERS = {"codex": codex, "claude": claude, "omp": omp}
ACTIONS = {
    "codex": frozenset({"archive", "trash"}),
    "claude": frozenset({"archive", "trash"}),
    "omp": frozenset(),
}
