"""Terminal view of ``booley eda installation|license list|show``.

Lives in the harness, not in :mod:`booley.eda`, because the table renderer and
the colour palette are harness modules and ``booley.eda`` must stay a separated
package that does not import the harness. The harness passes this module to
``booley.eda.cli.run`` as its ``record_view``; the module satisfies
``booley.eda.cli.RecordView``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from . import colors
from .terminal_table import render_table

#: Spaces between the longest label and its value in :func:`fields`.
_LABEL_GAP = 2


def table(
    records: Sequence[Mapping[str, object]],
    columns: Sequence[tuple[str, str]],
    *,
    empty: str,
    caps: Mapping[int, int],
) -> str:
    """Render *records* as a table of ``(header, field)`` *columns*.

    *caps* maps a column index to its visible-character cap. No records print
    the *empty* sentence instead of a header-only table.
    """
    if not records:
        return empty
    headers = [header for header, _field in columns]
    rows = [[str(record[field]) for _header, field in columns] for record in records]
    return "\n".join(render_table(headers, rows, max_widths=caps))


def fields(title: str, record: Mapping[str, object]) -> str:
    """Render one *record* as a bold title and aligned dim labels, never cutting values.

    A label is the field name with ``_`` as a space; ``name`` is in the title.
    """
    labels = {
        name.replace("_", " "): str(value) for name, value in record.items() if name != "name"
    }
    width = max(map(len, labels)) + _LABEL_GAP
    lines = [colors.bold_accent(f"{title} {record['name']}")]
    lines.extend(
        f"  {colors.dim(label)}{' ' * (width - len(label))}{value}"
        for label, value in labels.items()
    )
    return "\n".join(lines)
