from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import booley.domain.model as typed_model

    TYPED_MODEL = typed_model


def load_view() -> object:
    from booley.presentation import view

    return view
