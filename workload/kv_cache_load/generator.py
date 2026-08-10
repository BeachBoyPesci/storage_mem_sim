"""Generate MemoryEngine inputs for KV-cache loading workloads."""

from dataclasses import dataclass
from typing import List, TYPE_CHECKING

from ...memory_metrics import MemoryMetrics
from ...memory_type import MemoryRequestType
from .config import KVCacheLoadConfig, KVLoadGranularity
from .layout import KVPageLayout
from .metrics import KVCacheLoadStats
from .selector import KVTokenSelector

if TYPE_CHECKING:
    from ...memory_engine import MemoryEngine
    from ...memory_pool import MemoryPool


def _ordered_unique_page_ids(
    token_ids: List[int],
    layout: KVPageLayout,
) -> List[int]:
    """Return page IDs in first-touch order."""
    result: List[int] = []
    seen = set()
    for token_id in token_ids:
        page_id = layout.page_id(token_id)
        if page_id not in seen:
            seen.add(page_id)
            result.append(page_id)
    return result


@dataclass
class GeneratedKVCacheLoad:
    """Backend-independent MemoryEngine inputs and workload metadata."""

    addresses: List[int]
    sizes: List[int]
    request_types: List[MemoryRequestType]
    selected_token_ids: List[int]
    touched_page_ids: List[int]
    stats: KVCacheLoadStats

    def issue(self, engine: "MemoryEngine | MemoryPool") -> MemoryMetrics:
        """Submit this workload through the public issue_request API.

        Accepts both a MemoryEngine (local addresses) and a MemoryPool
        (global addresses); both expose ``issue_request(addr, size, type)``.
        """
        return engine.issue_request(
            self.addresses,
            self.sizes,
            self.request_types,
        )


class KVCacheLoadGenerator:
    """Translate KV access semantics into byte-address requests."""

    def __init__(self, selector: KVTokenSelector | None = None):
        self._selector = selector or KVTokenSelector()

    def generate(self, config: KVCacheLoadConfig) -> GeneratedKVCacheLoad:
        """Generate one KV-cache load without sorting or coalescing requests."""
        selected_tokens = self._selector.select(config)
        layout = KVPageLayout(
            base_addr=config.base_addr,
            token_size_bytes=config.token_size_bytes,
            page_size_tokens=config.page_size_tokens,
            page_alignment_bytes=config.page_alignment_bytes,
        )
        touched_pages = _ordered_unique_page_ids(selected_tokens, layout)

        if config.granularity is KVLoadGranularity.TOKEN:
            addresses = [
                layout.token_addr(token_id)
                for token_id in selected_tokens
            ]
            sizes = [config.token_size_bytes] * len(selected_tokens)
        elif config.granularity is KVLoadGranularity.PAGE:
            addresses = [
                layout.page_addr(page_id)
                for page_id in touched_pages
            ]
            sizes = [layout.page_data_bytes] * len(touched_pages)
        else:
            raise ValueError(
                f"unsupported KV load granularity: {config.granularity!r}"
            )

        request_types = [config.request_type] * len(addresses)
        demand_bytes = len(selected_tokens) * config.token_size_bytes
        issued_bytes = sum(sizes)
        unique_pages = len(touched_pages)

        if unique_pages:
            page_utilization = (
                len(selected_tokens)
                / (unique_pages * config.page_size_tokens)
            )
        else:
            page_utilization = 0.0
        read_amplification = (
            issued_bytes / demand_bytes if demand_bytes else 0.0
        )

        return GeneratedKVCacheLoad(
            addresses=addresses,
            sizes=sizes,
            request_types=request_types,
            selected_token_ids=selected_tokens,
            touched_page_ids=touched_pages,
            stats=KVCacheLoadStats(
                selected_tokens=len(selected_tokens),
                unique_pages=unique_pages,
                logical_requests=len(addresses),
                demand_bytes=demand_bytes,
                issued_bytes=issued_bytes,
                page_utilization=page_utilization,
                read_amplification=read_amplification,
            ),
        )

