"""The adapter registry. Adding a device family means writing one module here and listing its class
in ADAPTERS; nothing else in dobermann changes."""
from concurrent.futures import ThreadPoolExecutor

from dobermann.adapters.base import Adapter, Detection, SampleResult, run_sample  # noqa: F401
from dobermann.adapters.upnp_igd import UpnpIgd
from dobermann.adapters.zte_goform import ZteGoform

ADAPTERS = [UpnpIgd, ZteGoform]
BY_ID = {a.id: a for a in ADAPTERS}


def get(adapter_id):
    return BY_ID.get(adapter_id)


def detect_all(host, timeout=3.0):
    """Every adapter that recognises `host`, most confident first. Adapters are tried in parallel;
    one that crashes counts as 'not recognised', never as an error for the others."""
    def one(cls):
        try:
            return cls.detect(host, timeout=timeout)
        except Exception:
            return None
    with ThreadPoolExecutor(max_workers=len(ADAPTERS)) as pool:
        found = [d for d in pool.map(one, ADAPTERS) if d is not None]
    return sorted(found, key=lambda d: d.confidence, reverse=True)
