from .models import Md5Hash
from .parsers import parse_named_attribute, parse_named_attribute_pair


class BuildConfig:
    def __init__(
        self,
        root,
        install,
        install_size,
        download,
        download_size,
        encoding,
        encoding_size,
    ):
        self.root = root
        self.install = install
        self.install_size = install_size
        self.download = download
        self.download_size = download_size
        self.encoding = encoding
        self.encoding_size = encoding_size


def parse_build_config(data: bytes) -> BuildConfig:
    lines = data.strip().split(b"\n")
    # Skip comments and empty lines
    lines = [line for line in lines if not line.startswith(b"#") and line.strip()]

    root = parse_named_attribute(b"root", lines, value_type=Md5Hash)
    install = parse_named_attribute_pair(b"install", lines, value_type=Md5Hash)
    install_size = parse_named_attribute_pair(b"install-size", lines, value_type=int)
    download = parse_named_attribute_pair(b"download", lines, value_type=Md5Hash)
    download_size = parse_named_attribute_pair(b"download-size", lines, value_type=int)
    # Skip size and size-size in the rust code
    _ = parse_named_attribute_pair(b"size", lines, value_type=Md5Hash)
    _ = parse_named_attribute_pair(b"size-size", lines, value_type=int)
    encoding = parse_named_attribute_pair(b"encoding", lines, value_type=Md5Hash)
    encoding_size = parse_named_attribute_pair(b"encoding-size", lines, value_type=int)

    return BuildConfig(
        root=root,
        install=install,
        install_size=install_size,
        download=download,
        download_size=download_size,
        encoding=encoding,
        encoding_size=encoding_size,
    )
