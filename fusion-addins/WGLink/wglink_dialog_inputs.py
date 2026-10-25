"""Reading and building Fusion command inputs, shared by every WGLink dialog.

One owner for the small conversions each dialog needs: looking an input up,
reading a dropdown or a selection, applying selection filters an older Fusion
may not know, and millimetre values (Fusion value inputs hold centimetres
whatever unit they display). Main thread only, like the dialogs.
"""

from __future__ import annotations

import adsk.core

if __package__:
    from . import wglink_fem
else:
    import wglink_fem


def item(command_inputs: object, name: str) -> object | None:
    try:
        return command_inputs.itemById(name)
    except Exception:  # noqa: BLE001
        return None


def command_inputs(inputs: object) -> object:
    """Events for grouped inputs can supply only that group's collection."""
    command = getattr(inputs, "command", None)
    return command.commandInputs if command is not None else inputs


def value(command_inputs: object, name: str) -> object | None:
    found = item(command_inputs, name)
    return found.value if found else None


def selected_name(command_inputs: object, name: str, default: str) -> str:
    found = item(command_inputs, name)
    try:
        return str(found.selectedItem.name)
    except Exception:  # noqa: BLE001
        return default


def add_selection_filters(selection: object, *names: str) -> None:
    """Apply what this Fusion knows, rather than failing the whole dialog.

    A filter name an older Fusion does not recognise would otherwise raise
    before the execute handler is attached, leaving a button that does nothing.
    """

    for name in names:
        try:
            selection.addSelectionFilter(name)
        except Exception:  # noqa: BLE001
            pass


def selected_entities(command_inputs: object, name: str) -> list[object]:
    found = item(command_inputs, name)
    entities: list[object] = []
    try:
        for index in range(found.selectionCount):
            entities.append(found.selection(index).entity)
    except Exception:  # noqa: BLE001 - an empty selection is refused by the plan
        pass
    return entities


def mm_to_internal(value_mm: float) -> float:
    """Millimetres in Fusion's internal length unit (cm)."""
    return float(value_mm) / wglink_fem.CM_TO_MM


def mm_input(value_mm: float) -> object:
    return adsk.core.ValueInput.createByReal(mm_to_internal(value_mm))


def mm_value(command_inputs: object, name: str) -> float | None:
    found = item(command_inputs, name)
    try:
        return float(found.value) * wglink_fem.CM_TO_MM
    except Exception:  # noqa: BLE001 - an unreadable value is refused by the plan
        return None
