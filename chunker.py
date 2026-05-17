from dataclasses import dataclass
from typing import List

import tiktoken
from langchain_text_splitters import RecursiveCharacterTextSplitter


@dataclass
class Chunk:
    text: str
    index: int
    total: int
    token_count: int


CHUNK_CONFIG = {
    'caselaw':   {'chunk_size': 512,  'overlap': 64},
    'published': {'chunk_size': 512,  'overlap': 64},
    'private':   {'chunk_size': 2048, 'overlap': 128},
    'uncertain': {'chunk_size': 2048, 'overlap': 128},
}

LEGAL_SEPARATORS = [
    '\n\nI.', '\n\nII.', '\n\nIII.', '\n\nIV.', '\n\nV.',
    '\n\nA.', '\n\nB.', '\n\nC.',
    '\n\nFACTS', '\n\nANALYSIS', '\n\nHELD', '\n\nHOLDING',
    '\n\nCONCLUSION', '\n\nDISCUSSION', '\n\nBACKGROUND',
    '\n\n', '\n', ' ', '',
]

_enc = None


def _get_encoder():
    global _enc
    if _enc is None:
        _enc = tiktoken.get_encoding('cl100k_base')
    return _enc


def count_tokens(text: str) -> int:
    return len(_get_encoder().encode(text))


def chunk(text: str, doc_type: str) -> List[Chunk]:
    config = CHUNK_CONFIG.get(doc_type, CHUNK_CONFIG['private'])
    enc = _get_encoder()
    separators = LEGAL_SEPARATORS if doc_type == 'caselaw' else ['\n\n', '\n', ' ', '']

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=config['chunk_size'],
        chunk_overlap=config['overlap'],
        length_function=lambda t: len(enc.encode(t)),
        separators=separators,
    )

    splits = splitter.split_text(text)
    if not splits:
        splits = [text]

    chunks = [
        Chunk(
            text=s,
            index=i,
            total=len(splits),
            token_count=len(enc.encode(s)),
        )
        for i, s in enumerate(splits)
    ]
    return chunks
