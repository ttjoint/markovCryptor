"""Multi-channel steganography: Chinese prose, still photos, animated GIFs.

The convenience entry points below are the older single-channel API. For new
code use :class:`markovcryptor.engine.Engine`, which dispatches to every channel
and is what the CLI, the web API and the batch runner are built on.
"""

from .codec import CodecStats, MarkovStegoCodec, decode_with_models, identify_model
from .compression import TextModel
from .model import (
    MarkovModel,
    default_model_dir,
    discover_corpus_files,
    discover_model_paths,
    load_models,
    read_corpus_files,
    read_text_file,
    unique_model_path,
)
from .core.progress import Cancelled
from .punctuation import PunctuationModel, extract_hanzi

__all__ = [
    "Cancelled",
    "CodecStats",
    "MarkovModel",
    "MarkovStegoCodec",
    "PunctuationModel",
    "TextModel",
    "decode_with_models",
    "default_model_dir",
    "discover_corpus_files",
    "discover_model_paths",
    "extract_hanzi",
    "identify_model",
    "load_models",
    "read_corpus_files",
    "read_text_file",
    "unique_model_path",
]
__version__ = "0.3.1"
