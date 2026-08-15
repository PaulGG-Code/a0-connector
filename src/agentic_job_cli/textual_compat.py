from __future__ import annotations

from typing import Any

_TEXTUAL_INPUT_DECODER_GUARD_INSTALLED = False


def install_textual_linux_input_decoder_guard() -> None:
    """Keep Textual's Linux input thread alive when terminal bytes are malformed."""
    global _TEXTUAL_INPUT_DECODER_GUARD_INSTALLED

    if _TEXTUAL_INPUT_DECODER_GUARD_INSTALLED:
        return

    try:
        from textual.drivers import linux_driver
    except Exception:
        return

    original_getincrementaldecoder = getattr(linux_driver, "getincrementaldecoder", None)
    if not callable(original_getincrementaldecoder):
        return
    if getattr(original_getincrementaldecoder, "_a0_safe_utf8", False):
        _TEXTUAL_INPUT_DECODER_GUARD_INSTALLED = True
        return

    def getincrementaldecoder_with_safe_utf8(encoding: str) -> Any:
        decoder_class = original_getincrementaldecoder(encoding)
        normalized_encoding = str(encoding or "").replace("_", "-").lower()
        if normalized_encoding not in {"utf-8", "utf8"}:
            return decoder_class

        class SafeUTF8IncrementalDecoder(decoder_class):  # type: ignore[misc, valid-type]
            def __init__(self, errors: str = "strict") -> None:
                super().__init__("replace" if errors == "strict" else errors)

        SafeUTF8IncrementalDecoder.__name__ = decoder_class.__name__
        SafeUTF8IncrementalDecoder.__qualname__ = decoder_class.__qualname__
        return SafeUTF8IncrementalDecoder

    getincrementaldecoder_with_safe_utf8._a0_safe_utf8 = True  # type: ignore[attr-defined]
    getincrementaldecoder_with_safe_utf8._a0_original = original_getincrementaldecoder  # type: ignore[attr-defined]
    linux_driver.getincrementaldecoder = getincrementaldecoder_with_safe_utf8
    _TEXTUAL_INPUT_DECODER_GUARD_INSTALLED = True
