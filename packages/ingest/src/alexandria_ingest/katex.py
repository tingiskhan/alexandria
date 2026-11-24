"""Check formulas with KaTeX itself, so a formula that passes here renders in a KaTeX-based client.

`vendor/katex.min.js` is the unmodified dist file of the katex npm package (0.19.1, MIT). It runs in dukpy, an
embedded JavaScript interpreter, because the ingest image has no Node.
"""

from functools import lru_cache
from importlib.resources import files
from threading import Lock

import dukpy

_CHECK = """
(function () {
    try {
        katex.renderToString(dukpy.latex, {throwOnError: true, displayMode: dukpy.display, strict: 'ignore'});
        return null;
    } catch (error) {
        return String(error.message);
    }
})()
"""
_lock = Lock()  # an interpreter is not thread-safe, and Prefect runs tasks in threads


@lru_cache(maxsize=1)
def _interpreter() -> dukpy.JSInterpreter:
    interpreter = dukpy.JSInterpreter()
    source = files("alexandria_ingest").joinpath("vendor/katex.min.js").read_text(encoding="utf-8")
    interpreter.evaljs("var window = this; var self = this;" + source)
    return interpreter


def katex_error(latex: str, *, display: bool) -> str | None:
    """KaTeX's parse error for this formula, or None if it renders."""
    with _lock:
        return _interpreter().evaljs(_CHECK, latex=latex, display=display)
