"""Physical identity of a stored artifact: a hash of its exact bytes.

The counterpart to the semantic hash. Where
:mod:`quant_research_terminal.domain.dataset_identity` answers *what does this
data mean*, this module answers *which exact bytes are these* — and the two
answers are deliberately allowed to diverge:

* the same records written with a different compression codec or row-group size
  have the **same** semantic hash and a **different** physical hash;
* the same bytes copied to another path have the **same** physical hash, because
  a path is not part of an artifact's identity.

That divergence is the point. A physical hash detects corruption, truncation,
and replacement; a semantic hash detects a change in the data. Collapsing them
would make it impossible to tell a harmless re-encode from an edited dataset.

Read chunk-by-chunk rather than with ``Path.read_bytes`` so a large artifact is
never resident in memory in full. No cache, no thread pool, no optimisation
framework — just a loop.

What a hash does and does not prove
-----------------------------------
SHA-256 is used here as a deterministic content identity: it proves an artifact
is byte-identical to one previously seen. It is **not** a signature and carries
no claim about who produced the bytes or whether they are authentic.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final

from quant_research_terminal.domain.dataset_identity import PhysicalArtifactHash

#: Bytes read per iteration. Large enough that syscall overhead is irrelevant,
#: small enough that memory use is flat regardless of artifact size.
PHYSICAL_HASH_CHUNK_BYTES: Final[int] = 1 << 20


def physical_artifact_hash(path: Path) -> PhysicalArtifactHash:
    """Return the SHA-256 of the exact bytes stored at ``path``.

    Raises:
        FileNotFoundError: if the path does not exist. A missing artifact is a
            fact about the filesystem, not a contract violation, so it keeps its
            own exception type rather than being wrapped.
        OSError: if the file cannot be read.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(PHYSICAL_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return PhysicalArtifactHash(value=digest.hexdigest())
