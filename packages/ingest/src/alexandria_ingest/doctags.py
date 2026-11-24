"""Keep literal `<` in DocTags model output (Granite-Docling, SmolDocling) from eating the text after it.

Models emit DocTags with text unescaped: `<text>...x$_{<t}$ and more</text>`. docling-core's
`load_from_doctags` strips tags with a regex, reads `<t}$ and more</text>` as one tag, and silently drops it.
So `<` is swapped for a placeholder before parsing and swapped back after export.
"""

import re

from docling.pipeline.vlm_pipeline import VlmPipeline
from docling_core.types.doc import DoclingDocument
from docling_core.types.doc.tokens import DocumentToken
from PIL.Image import Image

# A Unicode noncharacter: never occurs in real text, and nothing in Docling touches it.
_PLACEHOLDER = "﷐"

# Numbered tags are matched by pattern, the rest are DocTags' fixed vocabulary. Code language tags look like <_Python_>.
_NUMBERED = re.compile(r"_\d+$")
_NAMES = sorted({t.strip("</>") for t in DocumentToken.get_special_tokens() if not _NUMBERED.search(t.rstrip(">"))})
_LITERAL_LESS_THAN = re.compile(
    rf"<(?!/?(?:{'|'.join(_NAMES)})>|/?(?:loc|section_header_level|key|value|link)_\d+>|_[^<>]*_>)"
)


def hide_literal_less_than(doctags: str) -> str:
    """Replace every `<` that does not open a DocTags tag with the placeholder."""
    return _LITERAL_LESS_THAN.sub(_PLACEHOLDER, doctags)


def restore_literal_less_than(text: str) -> str:
    """Undo `hide_literal_less_than` in text exported from the document."""
    return text.replace(_PLACEHOLDER, "<")


class LiteralTextVlmPipeline(VlmPipeline):
    """VlmPipeline that keeps literal `<`; export with `restore_literal_less_than`.

    Overrides a private Docling method, the only point between the model's output and the parser.
    """

    @staticmethod
    def _doctags_page_document(predicted_text: str, image: Image) -> DoclingDocument:
        return VlmPipeline._doctags_page_document(hide_literal_less_than(predicted_text), image)
