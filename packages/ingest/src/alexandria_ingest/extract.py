"""Extractors: document bytes (PDF, Word, PowerPoint, Excel, HTML, Markdown, images, transcripts) to per-page Markdown.

`make_extractor` builds the one a sync uses: a FormatRouter that sends transcripts (.vtt, .srt) to the
TranscriptExtractor and everything else to Docling.
"""

import re
from collections.abc import Iterable, Mapping
from io import BytesIO

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions, VlmConvertOptions, VlmPipelineOptions
from docling.document_converter import DocumentConverter, FormatOption, ImageFormatOption, PdfFormatOption
from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline
from docling_core.types.doc import DocItemLabel, DoclingDocument
from docling_core.types.doc.document import DOCUMENT_TOKENS_EXPORT_LABELS
from docling_core.types.io import DocumentStream

from alexandria_core.models import Page
from alexandria_core.protocols import Extractor
from alexandria_ingest.doctags import LiteralTextVlmPipeline, restore_literal_less_than
from alexandria_ingest.latex import normalize_math
from alexandria_ingest.settings import DEFAULT_FORMATS, TRANSCRIPT_FORMATS, ExtractSettings, extension, input_format
from alexandria_ingest.transcript import TranscriptExtractor

# C0 control characters and DEL, except tab and newline. PDFs with broken font encodings turn glyphs into these;
# they carry no meaning, and Postgres text cannot hold NUL at all.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


# What Docling exports by default, without page numbers and running footers: noise at the end of every page.
_EXPORTED_LABELS = DOCUMENT_TOKENS_EXPORT_LABELS - {DocItemLabel.PAGE_FOOTER}


def clean_text(text: str) -> str:
    """Remove control characters (keeping tab and newline) so the text is storable and searchable."""
    return _CONTROL.sub("", text)


class DoclingExtractor:
    """Documents to per-page Markdown with Docling: OCR for scanned pages and images, table structure recognition on.

    Reads the file extensions in `formats` (see ExtractSettings). PDFs and PowerPoint slides keep their pages,
    Excel sheets become pages, and formats without pages (Word, HTML, Markdown, text) come out as one page.
    Docling picks the GPU when one is available and falls back to CPU.
    """

    def __init__(
        self,
        formats: Iterable[str] = DEFAULT_FORMATS - TRANSCRIPT_FORMATS,
        *,
        use_vlm: bool = True,
        vlm_preset: str = "mineru2_pro",
    ) -> None:
        allowed = sorted({input_format(extension) for extension in formats}, key=lambda f: f.value)

        options = PdfPipelineOptions(do_ocr=True, do_table_structure=True, do_formula_enrichment=True)
        pdf_options = VlmPipelineOptions(vlm_options=VlmConvertOptions.from_preset(vlm_preset)) if use_vlm else options

        format_options: dict[InputFormat, FormatOption] = {
            InputFormat.PDF: PdfFormatOption(
                pipeline_options=pdf_options, pipeline_cls=LiteralTextVlmPipeline if use_vlm else StandardPdfPipeline
            ),
            InputFormat.IMAGE: ImageFormatOption(pipeline_options=options),
        }

        self._converter = DocumentConverter(
            allowed_formats=allowed, format_options={f: o for f, o in format_options.items() if f in allowed}
        )

    def extract(self, content: bytes, name: str) -> list[Page]:
        """Convert one document; `name` (the file name) picks the format. Tables come out as Markdown tables."""
        result = self._converter.convert(DocumentStream(name=name, stream=BytesIO(content)))
        document = result.document
        if not document.pages:
            return [Page(number=1, markdown=_markdown(document))]
        return [Page(number=n, markdown=_markdown(document, n)) for n in sorted(document.pages)]


class FormatRouter:
    """An Extractor that hands each file to the extractor for its extension (lower-case, without the dot)."""

    def __init__(self, extractors: Mapping[str, Extractor]) -> None:
        self.extractors = dict(extractors)

    def extract(self, content: bytes, name: str) -> list[Page]:
        """Extract with the extractor for `name`'s extension. Raises ValueError for an extension it has none for."""
        extractor = self.extractors.get(extension(name))
        if extractor is None:
            raise ValueError(f"no extractor for {name!r}; extensions: {', '.join(sorted(self.extractors))}")
        return extractor.extract(content, name)


def make_extractor(settings: ExtractSettings) -> FormatRouter:
    """The extractor for `settings.formats`: transcripts by a TranscriptExtractor, the rest by one DoclingExtractor.

    Docling is only set up when a format needs it.
    """
    extractors: dict[str, Extractor] = {}
    if docling_formats := settings.formats - TRANSCRIPT_FORMATS:
        extractors |= dict.fromkeys(docling_formats, DoclingExtractor(docling_formats))
    if transcript_formats := settings.formats & TRANSCRIPT_FORMATS:
        extractors |= dict.fromkeys(transcript_formats, TranscriptExtractor(settings.transcript_block_minutes))
    return FormatRouter(extractors)


def _markdown(document: DoclingDocument, page_no: int | None = None) -> str:
    """One page, or the whole document, as storable Markdown."""
    # Docling escapes for HTML rendering by default ("S&amp;P"); readers here are the agent and full-text search.
    exported = document.export_to_markdown(page_no=page_no, escape_html=False, labels=_EXPORTED_LABELS)
    return clean_text(normalize_math(restore_literal_less_than(exported)))
