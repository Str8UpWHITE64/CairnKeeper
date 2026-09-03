"""Self-signed CA + on-demand leaf certificates for the MITM proxy.

The game is launched with SSL peer verification disabled (``bVerifyPeer=False``),
so these certificates only need to be well-formed -- they are never trusted by
anything outside the game process, and nothing is installed into the Windows
certificate store.
"""
from __future__ import annotations

import datetime as _dt
import ipaddress
import threading
from pathlib import Path

# Imported where it is used rather than here. Certificates are only needed by
# the TLS proxy, and the proxy is only needed when the game has not been
# patched -- which the supervisor always does. Keeping this out of the import
# path means the client is pure standard library for everyone who just plays:
# a smaller download, no compiled crypto to bundle, and one less thing for a
# virus scanner to take an interest in.

_EPOCH = _dt.datetime(2020, 1, 1, tzinfo=_dt.timezone.utc)
_EXPIRY = _dt.datetime(2035, 1, 1, tzinfo=_dt.timezone.utc)


def _crypto():
    """The certificate toolkit, loaded only if a certificate is wanted."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    return x509, hashes, serialization, rsa, NameOID


class CertStore:
    """Generates and caches a CA plus per-hostname leaf certificates."""

    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()   # _ca() is called from inside cert_for
        self._ca_key_path = self.dir / "ca.key"
        self._ca_cert_path = self.dir / "ca.crt"
        # Built on demand, not here. Constructing this eagerly meant every
        # server -- including the offline one that never terminates TLS --
        # needed the certificate toolkit, which the packaged build leaves out
        # on purpose. A frozen client died at "Starting the local server" for
        # a certificate nothing was going to ask for.
        self._ca_key = None
        self._ca_cert = None

    def _ca(self):
        """The CA, made the first time something actually wants a certificate."""
        with self._lock:
            if self._ca_key is None:
                self._ca_key, self._ca_cert = self._load_or_make_ca()
            return self._ca_key, self._ca_cert

    # ---------------------------------------------------------------- CA

    def _load_or_make_ca(self):
        x509, hashes, serialization, rsa, NameOID = _crypto()
        if self._ca_key_path.exists() and self._ca_cert_path.exists():
            key = serialization.load_pem_private_key(
                self._ca_key_path.read_bytes(), password=None
            )
            cert = x509.load_pem_x509_certificate(self._ca_cert_path.read_bytes())
            return key, cert

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name(
            [
                x509.NameAttribute(NameOID.COMMON_NAME, "Phantom Abyss Offline CA"),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "PhantomAbyss-Offline"),
            ]
        )
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_EPOCH)
            .not_valid_after(_EXPIRY)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .sign(key, hashes.SHA256())
        )

        self._ca_key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
        self._ca_cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        return key, cert

    @property
    def ca_cert_path(self) -> Path:
        """Where the CA lives, making it first if nobody has needed one yet.

        Asking for the path is asking for the file: the launcher hands it to
        the game so an unpatched client can trust the proxy, and a path to a
        file that does not exist would be a silent failure there.
        """
        self._ca()
        return self._ca_cert_path

    # -------------------------------------------------------------- leaf

    def cert_for(self, hostname: str) -> tuple[Path, Path]:
        """Return (cert_path, key_path) for *hostname*, generating if needed."""
        x509, hashes, serialization, rsa, NameOID = _crypto()
        safe = hostname.replace(":", "_").replace("*", "_wildcard_")
        cert_path = self.dir / f"{safe}.crt"
        key_path = self.dir / f"{safe}.key"

        with self._lock:
            if cert_path.exists() and key_path.exists():
                return cert_path, key_path

            ca_key, ca_cert = self._ca()
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

            try:
                alt: x509.GeneralName = x509.IPAddress(ipaddress.ip_address(hostname))
            except ValueError:
                alt = x509.DNSName(hostname)

            cert = (
                x509.CertificateBuilder()
                .subject_name(
                    x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname[:64])])
                )
                .issuer_name(ca_cert.subject)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(_EPOCH)
                .not_valid_after(_EXPIRY)
                .add_extension(x509.SubjectAlternativeName([alt]), critical=False)
                .add_extension(
                    x509.BasicConstraints(ca=False, path_length=None), critical=True
                )
                .sign(ca_key, hashes.SHA256())
            )

            key_path.write_bytes(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.TraditionalOpenSSL,
                    serialization.NoEncryption(),
                )
            )
            cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            return cert_path, key_path
