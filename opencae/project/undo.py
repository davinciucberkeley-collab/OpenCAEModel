"""Snapshot-based undo/redo for project edits.

Every user edit goes through :meth:`UndoStack.push`, which stores a deep copy
of the project *before* the change together with a description. Heavy data
(meshes, results) lives in files and is not duplicated; their staleness is
tracked through hashes in the model.
"""
from __future__ import annotations

from dataclasses import dataclass

from opencae.project.model import Project


@dataclass
class _Entry:
    description: str
    state: Project


class UndoStack:
    def __init__(self, limit: int = 100) -> None:
        self._undo: list[_Entry] = []
        self._redo: list[_Entry] = []
        self.limit = limit

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()

    def push(self, description: str, before: Project) -> None:
        self._undo.append(_Entry(description, before.clone()))
        if len(self._undo) > self.limit:
            self._undo.pop(0)
        self._redo.clear()

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo_text(self) -> str:
        return self._undo[-1].description if self._undo else ""

    def redo_text(self) -> str:
        return self._redo[-1].description if self._redo else ""

    def undo(self, current: Project) -> Project:
        e = self._undo.pop()
        self._redo.append(_Entry(e.description, current.clone()))
        return e.state

    def redo(self, current: Project) -> Project:
        e = self._redo.pop()
        self._undo.append(_Entry(e.description, current.clone()))
        return e.state
