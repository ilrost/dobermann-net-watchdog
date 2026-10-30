"""What dobermann needs from the system, how to tell what is missing, and the package that provides it
on each family of distributions.

No Python package is needed: dobermann uses the standard library only. These are programs:

  python3          the program itself (3.11 or newer)
  ping             every measurement
  ip               the gateway and this machine's addresses
  openssl          the panel's HTTPS certificate                    (only with HTTPS)
  ca-certificates  checking the mail provider's certificate
  avahi            the name dobermann.local on the home network     (only with the local name)

systemd is required too, but it is not a package one installs: a system without it is not supported.
"""
import os
import shutil
import subprocess

# package manager -> (install command, {dependency: [packages]})
MANAGERS = {
    "apt": (["apt-get", "install", "-y", "--no-install-recommends"], {
        "python3": ["python3"], "ping": ["iputils-ping"], "ip": ["iproute2"], "openssl": ["openssl"],
        "ca-certificates": ["ca-certificates"],
        "avahi": ["avahi-daemon", "avahi-utils"]}),
    "dnf": (["dnf", "install", "-y"], {
        "python3": ["python3"], "ping": ["iputils"], "ip": ["iproute"], "openssl": ["openssl"],
        "ca-certificates": ["ca-certificates"],
        "avahi": ["avahi", "avahi-tools"]}),
    "pacman": (["pacman", "-S", "--needed", "--noconfirm"], {
        "python3": ["python"], "ping": ["iputils"], "ip": ["iproute2"], "openssl": ["openssl"],
        "ca-certificates": ["ca-certificates"], "avahi": ["avahi"]}),
    "zypper": (["zypper", "--non-interactive", "install"], {
        "python3": ["python3"], "ping": ["iputils"], "ip": ["iproute2"], "openssl": ["openssl"],
        "ca-certificates": ["ca-certificates"],
        "avahi": ["avahi", "avahi-utils"]}),
}
REQUIRED = ("python3", "ping", "ip", "ca-certificates")
OPTIONAL = ()       # none at the moment; install() keeps the way to have one
CA_BUNDLES = ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt",
              "/etc/ssl/ca-bundle.pem", "/etc/ssl/cert.pem")


def wanted(https=True, mdns=True):
    """The dependencies of an install with these choices, in a stable order."""
    out = list(REQUIRED)
    if https:
        out.append("openssl")
    if mdns:
        out.append("avahi")
    return out


def optional(dep):
    """A dependency whose absence is a warning, not a stop."""
    return dep in OPTIONAL


def present(dep, which=shutil.which, exists=os.path.exists):
    if dep == "python3":
        return which("python3") is not None
    if dep == "ca-certificates":
        return any(exists(p) for p in CA_BUNDLES)
    if dep == "avahi":
        return (which("avahi-daemon") is not None or exists("/usr/sbin/avahi-daemon")) and \
            which("avahi-publish-address") is not None
    return which(dep) is not None


def missing(deps, **kw):
    return [d for d in deps if not present(d, **kw)]


def manager(which=shutil.which):
    """The package manager of this system ('apt', 'dnf', 'pacman', 'zypper'), or None."""
    for name, probe in (("apt", "apt-get"), ("dnf", "dnf"), ("pacman", "pacman"), ("zypper", "zypper")):
        if which(probe):
            return name
    return None


def packages(deps, mgr):
    names = []
    for d in deps:
        for p in MANAGERS[mgr][1][d]:
            if p not in names:
                names.append(p)
    return names


def install_command(deps, mgr):
    return MANAGERS[mgr][0] + packages(deps, mgr)


def install(deps, mgr, run=subprocess.run):
    """Installs the packages of `deps`, the optional ones one by one so that a package this distribution
    does not have does not stop the others. Returns the deps that failed."""
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")   # no installer may stop to ask a question
    if mgr == "apt":
        run(["apt-get", "update", "-q"], env=env, check=False, capture_output=True)
    failed = []
    must = [d for d in deps if not optional(d)]
    if must and run(install_command(must, mgr), env=env, check=False).returncode != 0:
        failed += [d for d in must if not present(d)]     # only what is really still missing
    for d in deps:
        if optional(d) and run(install_command([d], mgr), env=env, check=False).returncode != 0:
            failed.append(d)
    return failed


def has_systemd(exists=os.path.exists):
    return exists("/run/systemd/system")
