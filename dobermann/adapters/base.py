"""The adapter contract: the one interface every router, modem or gateway is reached through.

An adapter translates one family of devices (a protocol, or a vendor's own API) into facts from
`dobermann.facts`. The rest of dobermann never imports a specific adapter: it asks the registry
which adapters recognise a host (`detect`) and then only calls `sample()`.

Rules an adapter must follow (docs/ADAPTERS.md explains them for someone writing a new one):
  * detect() is quick, read-only and never needs credentials or a login.
  * sample() returns facts, or {} when the device does not answer. Never raises, never hangs:
    every network call has a short timeout. `run_sample()` enforces this anyway.
  * Credentials are optional unless `needs_credentials` is True, and an adapter that works with
    them must still return what it can without them.
  * `single_session = True` means logging in can push the user out of the device's own web page.
    Such an adapter must give the session back (stop logging in for a while) when it notices
    someone else took it.
"""
import time
from dataclasses import dataclass, field

from dobermann import facts as factlib
from dobermann.i18n import _

LEVEL_STANDARD = 2      # a documented protocol (UPnP IGD, TR-064, SNMP...)
LEVEL_UNOFFICIAL = 3    # a vendor's undocumented web API; optional, may break with a firmware update


@dataclass
class Detection:
    """What detect() found: how sure it is (0..1) and what it can say without credentials."""
    adapter_id: str
    confidence: float
    vendor: str = None
    model: str = None
    firmware: str = None
    notes: list = field(default_factory=list)
    options: dict = field(default_factory=dict)  # what sample() needs to reach it again (URLs, ports)


@dataclass
class SampleResult:
    facts: dict
    ok: bool                    # the device answered at all
    error: str = None           # why not, for logs and the configurator; never shown as a fact
    dropped: list = field(default_factory=list)  # names the adapter emitted outside its contract
    duration_s: float = 0.0


class Adapter:
    id = None                   # unique; what chain.json stores in a node's "adapter"
    name = None                 # shown to the user
    level = LEVEL_STANDARD
    needs_credentials = False   # True: useless without them
    accepts_credentials = False # True: credentials add detail
    single_session = False
    provides = frozenset()      # fact names this adapter may emit

    @classmethod
    def detect(cls, host, timeout=3.0):
        """A Detection if `host` looks like a device this adapter can read, else None."""
        raise NotImplementedError

    def __init__(self, host, credentials=None, options=None, timeout=4.0):
        self.host = host
        self.credentials = credentials or {}
        self.options = options or {}
        self.timeout = timeout

    def sample(self):
        """A dict of facts. {} when the device did not answer."""
        raise NotImplementedError

    def status(self):
        """Adapter-specific state worth showing in the configurator (e.g. 'session given back to
        the user until 14:32'). Plain strings, no secrets."""
        return {}


def run_sample(adapter):
    """sample() with the contract enforced: an exception becomes an error, facts are validated
    against the vocabulary and the adapter's own `provides`."""
    t0 = time.monotonic()
    try:
        raw = adapter.sample()
        error = None
    except Exception as e:  # the contract says never raise; a buggy adapter must not stop sampling
        raw, error = {}, f"{type(e).__name__}: {e}"
    clean, dropped = factlib.validate(raw, allowed=adapter.provides)
    if error is None and not clean:
        error = _("nessuna risposta dall'apparato")
    return SampleResult(facts=clean, ok=bool(clean), error=error, dropped=dropped,
                        duration_s=round(time.monotonic() - t0, 3))
