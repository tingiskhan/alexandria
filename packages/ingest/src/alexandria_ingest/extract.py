"""Extractor: document bytes (PDF, Word, PowerPoint, Excel, HTML, Markdown, images…) to per-page Markdown."""

import re
from collections.abc import Iterable
from io import BytesIO

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, FormatOption, ImageFormatOption, PdfFormatOption
from docling_core.types.io import DocumentStream

from alexandria_core.models import Page
from alexandria_ingest.settings import DEFAULT_FORMATS, input_format

# C0 control characters and DEL, except tab and newline. PDFs with broken font encodings turn glyphs into these;
# they carry no meaning, and Postgres text cannot hold NUL at all.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean_text(text: str) -> str:
    """Remove control characters (keeping tab and newline) so the text is storable and searchable."""
    return _CONTROL.sub("", text)


class DoclingExtractor:
    """Documents to per-page Markdown with Docling: OCR for scanned pages and images, table structure recognition on.

    Reads the file extensions in `formats` (see ExtractSettings). PDFs and PowerPoint slides keep their pages,
    Excel sheets become pages, and formats without pages (Word, HTML, Markdown, text) come out as one page.
    Docling picks the GPU when one is available and falls back to CPU.
    """

    def __init__(self, formats: Iterable[str] = DEFAULT_FORMATS) -> None:
        allowed = sorted({input_format(extension) for extension in formats}, key=lambda f: f.value)
        options = PdfPipelineOptions(do_ocr=True, do_table_structure=True)
        format_options: dict[InputFormat, FormatOption] = {
            InputFormat.PDF: PdfFormatOption(pipeline_options=options),
            InputFormat.IMAGE: ImageFormatOption(pipeline_options=options),
        }
        self._converter = DocumentConverter(
            allowed_formats=allowed, format_options={f: o for f, o in format_options.items() if f in allowed}
        )

    def extract(self, content: bytes, name: str) -> list[Page]:
        """Convert one document; `name` (the file name) picks the format. Tables come out as Markdown tables."""
        result = self._converter.convert(DocumentStream(name=name, stream=BytesIO(content)))
        document = result.document
        # Docling escapes for HTML rendering by default ("S&amp;P"); readers here are the agent and full-text search.
        if not document.pages:
            return [Page(number=1, markdown=clean_text(document.export_to_markdown(escape_html=False)))]
        return [
            Page(number=n, markdown=clean_text(document.export_to_markdown(page_no=n, escape_html=False)))
            for n in sorted(document.pages)
        ]
