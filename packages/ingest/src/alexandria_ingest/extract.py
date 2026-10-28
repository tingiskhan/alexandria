"""Extractor: PDF bytes to per-page Markdown, using Docling."""

import re
from io import BytesIO

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.types.io import DocumentStream

from alexandria_core.models import Page

# C0 control characters and DEL, except tab and newline. PDFs with broken font encodings turn glyphs into these;
# they carry no meaning, and Postgres text cannot hold NUL at all.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean_text(text: str) -> str:
    """Remove control characters (keeping tab and newline) so the text is storable and searchable."""
    return _CONTROL.sub("", text)


class DoclingExtractor:
    """PDF to per-page Markdown with Docling: OCR for scanned pages, table structure recognition on.

    Docling picks the GPU when one is available and falls back to CPU.
    """

    def __init__(self) -> None:
        options = PdfPipelineOptions(do_ocr=True, do_table_structure=True)
        self._converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})

    def extract(self, pdf: bytes) -> list[Page]:
        """Convert one PDF. Pages are numbered from 1; tables come out as Markdown tables."""
        result = self._converter.convert(DocumentStream(name="document.pdf", stream=BytesIO(pdf)))
        document = result.document
        # Docling escapes for HTML rendering by default ("S&amp;P"); readers here are the agent and full-text search.
        return [
            Page(number=n, markdown=clean_text(document.export_to_markdown(page_no=n, escape_html=False)))
            for n in sorted(document.pages)
        ]
